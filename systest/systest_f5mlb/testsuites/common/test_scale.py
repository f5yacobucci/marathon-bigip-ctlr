# Copyright 2017 F5 Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test suite to verify scale test scenarios."""


import json
import multiprocessing
import re
import time
from copy import deepcopy

from pytest import meta_suite, meta_test, fixture
from pytest import symbols

from . import utils


pytestmark = meta_suite(tags=["scale", "marathon", "k8s", "openshift"])

F5MLB_CPUS = 0.5
F5MLB_MEM = 128
SVC_CPUS = 0.01
SVC_MEM = 32
SVC_TIMEOUT = 10 * 60
SVC_START_PORT = 7000
VS_INTERVAL = 10
VS_TIMEOUT = 10 * 60
DEFAULT_SSH_TIMEOUT = 3 * 60
DEFAULT_SCALE_PERF_ENV_VARS = {'SCALE_PERF_ENABLE': True}


@fixture(scope="module", autouse=True)
def _envInfo(ssh):
    for worker in symbols.workers:
        envInfo = "\nEnvironment Info:\n  "
        envInfo += worker + "\n  "
        envInfo += "Linux kernel: "
        envInfo += ssh.run(worker, "uname -r",
                           timeout=DEFAULT_SSH_TIMEOUT) + "\n  "
        envInfo += ssh.run(worker, "sudo docker --version",
                           timeout=DEFAULT_SSH_TIMEOUT) + "\n  "
        envInfo += ("Environment MemTotal: " +
                    ssh.run(worker,
                            "awk '/MemTotal/ {print $2}' /proc/meminfo",
                            timeout=DEFAULT_SSH_TIMEOUT) + " KB\n  ")
        envInfo += "Environment CPU:\n    "
        envInfo += ssh.run(worker, "lscpu | sed 's/^/    /'",
                           timeout=DEFAULT_SSH_TIMEOUT)
        envInfo += ("\n    " +
                    ssh.run(worker,
                            "cat /proc/cpuinfo | grep -i 'model name' | uniq",
                            timeout=DEFAULT_SSH_TIMEOUT))
        print envInfo


@meta_test(id="f5mlb-59", tags=[])
def test_bigip_controller1_svc10_srv100(ssh, orchestration, scale_controller):
    """Scale: 1 bigip-controller, 10 managed svcs (w/ 100 backends each).

    Each managed service has 100 backend servers.
    So this test creates 1,011 application instances.
    """
    _run_scale_test(ssh, orchestration, num_svcs=10, num_srvs=100)


@meta_test(id="f5mlb-60", tags=["no_regression"])
def test_bigip_controller1_svc100_srv10(ssh, orchestration, scale_controller):
    """Scale: 1 bigip-controller, 100 managed svcs (w/ 10 backends each).

    Each managed service has 10 backend servers.
    So this test creates 1,101 application instances.
    """
    _run_scale_test(ssh, orchestration, num_svcs=100, num_srvs=10)


@meta_test(id="f5mlb-61", tags=["no_regression"])
def test_bigip_controller1_svc100_srv100(ssh, orchestration, scale_controller):
    """Scale: 1 bigip-controller, 100 managed svcs (w/ 100 backends each).

    Each managed service has and 100 backend servers.
    So this test creates 10,101 application instances.
    """
    _run_scale_test(ssh, orchestration, num_svcs=100, num_srvs=100)


@meta_test(id="f5mlb-69", tags=[])
def test_bigip_controller_application_deployment_sizing(
        ssh, orchestration, request):
    """Scale: 1 bigip-controller, 10 managed svcs (w/ 100 backends each).

    Test the time it takes to deploy the bigip-controller and for it to
    configure the bigip.
    """
    _run_deployment_sizing_test(
        ssh, orchestration, request, num_svcs=10, num_srvs=100)


@meta_test(id="f5mlb-70", tags=[])
def test_bigip_controller_application_scaling_sizing(
        ssh, orchestration, request):
    """Scale: 1 bigip-controller, 10 managed svcs (w/ 100 backends each).

    Test the time it takes to reconfigure the bigip when tasks are scaled down
    by a factor of 50%.
    """
    _run_scaling_sizing_test(
        ssh, orchestration, request, num_svcs=10, num_srvs=100)


def _run_scale_test(
        ssh, orchestration, num_svcs, num_srvs):

    print '_run_scale_test: STARTED'
    # - first, scale-up the appropriate services and instances
    svcs = _scale_svcs(ssh, orchestration, num_svcs, num_srvs, True)
    print '_run_scale_test: SVCS CREATED'

    # - set pool_size to number of cores on the bastion
    pool_size = 4
    # - then, verify round-robin load balancing for each service
    for slice in [svcs[i:i+pool_size] for i in range(0, len(svcs), pool_size)]:
        p = multiprocessing.Pool(processes=len(slice))
        p.map(_verify_bigip_controller, slice)
        p.close()
        p.join()
    print '_run_scale_test: FINISHED'


def _run_deployment_sizing_test(
        ssh, orchestration, request, num_svcs, num_srvs):
    print '_run_deployment_sizing_test: STARTED'
    tot_srvs = num_svcs * num_srvs
    svcs = _scale_svcs(ssh, orchestration, num_svcs, num_srvs, False)
    print '_run_deployment_sizing_test: SVCS CREATED'

    # - check log for configuration finished
    ctlr = utils.deploy_controller(request, orchestration,
                                   env_vars=DEFAULT_SCALE_PERF_ENV_VARS,
                                   mode=utils.POOL_MODE_CLUSTER)
    ctlr_instance = utils.get_app_instance(ctlr)
    print '_run_deployment_sizing_test: CTLR DEPLOYED'
    start_str = 'SCALE_PERF: Started controller at: '
    for ctlr_log in utils.check_logs(ctlr_instance, start_str):
        start_time = float(ctlr_log)
        if start_time is not None:
            break
    print '_run_deployment_sizing_test: INITIAL LOG FOUND'

    start_str = 'SCALE_PERF: Test data: '
    stop_time = _verify_scale_perf_log_data(ctlr_instance, start_str, svcs,
                                            num_srvs, tot_srvs)
    print '_run_deployment_sizing_test: FINAL LOG FOUND'

    orchestration.namespace = utils.controller_namespace()
    ctlr.delete()
    print '_run_deployment_sizing_test: CTLR DELETED'

    dur = stop_time - start_time
    objs = tot_srvs + num_svcs + 1
    res = objs / dur

    print '\nTime elapsed:        %f' % dur
    print 'Objects added:        %d' % objs
    print "Controller deployed and configured %f objs/sec" % res


def _run_scaling_sizing_test(
        ssh, orchestration, request, num_svcs, num_srvs):
    print '_run_scaling_sizing_test: STARTED'
    tot_srvs = num_svcs * num_srvs
    svcs = _scale_svcs(ssh, orchestration, num_svcs, num_srvs, False)
    print '_run_scaling_sizing_test: SVCS CREATED'

    # - check log for initial configuration finished
    ctlr = utils.deploy_controller(request, orchestration,
                                   env_vars=DEFAULT_SCALE_PERF_ENV_VARS,
                                   mode=utils.POOL_MODE_CLUSTER)
    ctlr_instance = utils.get_app_instance(ctlr)
    print '_run_scaling_sizing_test: CTLR DEPLOYED'
    start_str = 'SCALE_PERF: Test data: '
    start_time = _verify_scale_perf_log_data(ctlr_instance, start_str, svcs,
                                             num_srvs, tot_srvs)
    print '_run_scaling_sizing_test: INITIAL LOG FOUND'

    # - scale-down services by 50%
    svc_name = svcs[0]['svc_name']
    num_scale = num_srvs / 2
    orchestration.app.scale(svc_name, num_scale, timeout=SVC_TIMEOUT)
    print '_run_scaling_sizing_test: APP SCALED DOWN'

    # - check log for final configuration finished
    tot_srvs -= num_scale
    stop_time = _verify_scale_perf_log_data(ctlr_instance, start_str, svcs,
                                            num_srvs, tot_srvs,
                                            scaled_svc=(svc_name, num_scale))
    print '_run_scaling_sizing_test: FINAL LOG FOUND'

    orchestration.namespace = utils.controller_namespace()
    ctlr.delete()
    print '_run_scaling_sizing_test: CTLR DELETED'

    dur = stop_time - start_time
    objs = num_scale
    res = objs / dur

    print '\nTime elapsed:         %f' % dur
    print 'Objects removed:      %d' % objs
    print 'Controller deployed and configured %f objs/sec' % res


def _scale_svcs(
        ssh, orchestration, num_svcs, num_srvs, wait_for_vs,
        svc_cpus=SVC_CPUS, svc_mem=SVC_MEM, timeout=SVC_TIMEOUT):
    svc_inputs = []
    svcs = []
    for i in range(1, num_svcs + 1):
        svc_inputs.append({
            'idx': i,
            'ssh': ssh,
            'orchestration': orchestration,
            'num_srvs': num_srvs,
            'svc_cpus': svc_cpus,
            'svc_mem': svc_mem,
            'timeout': timeout,
            'wait_for_vs': wait_for_vs
        })
    pool_size = 10
    slices = [
        svc_inputs[i:i+pool_size] for i in range(0, len(svc_inputs), pool_size)
    ]
    print '_scale_svcs: CALL multiprocessing'
    for slice in slices:
        p = multiprocessing.Pool(processes=len(slice))
        svcs += p.map(_create_svc, slice)
        p.close()
        p.join()
    return svcs


def _verify_scale_perf_log_data(ctlr_instance, start_str, svcs,
                                backend_per_svc, tot_backends,
                                scaled_svc=None):
    for log in utils.check_logs(ctlr_instance, start_str):
        data = json.loads(log)
        if (len(svcs) == data.get('Total_Services') and
                data.get('Total_Backends') == tot_backends):
            valid_backends = True
            for svc in svcs:
                if backend_per_svc != data[svc['svc_name']]:
                    if scaled_svc is not None:
                        (svc_name, scale_factor) = scaled_svc
                        if data[svc_name] != scale_factor:
                            valid_backends = False
                    else:
                        valid_backends = False
            if valid_backends:
                data_time = data.get('Time')
                return data_time


def _create_svc(kwargs):
    # - create a managed service
    svc_name = "svc-%d" % kwargs['idx']
    config = _get_scale_config(kwargs)
    print '%s: _create_svc: CALL create_managed_northsouth_service' % svc_name
    svc = utils.create_managed_northsouth_service(
        kwargs['orchestration'],
        svc_name,
        cpus=kwargs['svc_cpus'],
        mem=kwargs['svc_mem'],
        timeout=kwargs['timeout'],
        num_instances=kwargs['num_srvs'],
        config=config
    )
    if kwargs['wait_for_vs'] is True:
        print '%s: _create_svc: CALL _wait_for_virtual_server' % svc_name
        _wait_for_virtual_server(svc, kwargs['ssh'])
    return {
        'svc_name': svc_name,
        'ssh': kwargs['ssh'],
        'orchestration': kwargs['orchestration']
    }


def _wait_for_virtual_server(svc, ssh, timeout=VS_TIMEOUT):
    duration = 0
    interval = VS_INTERVAL

    vs_name = utils.get_backend_object_name(svc)
    vs_url = (
        "https://%s/mgmt/tm/ltm/virtual/~%s~%s/stats"
        % (
            symbols.bigip_mgmt_ip,
            utils.DEFAULT_F5MLB_PARTITION,
            vs_name
        )
    )
    curl_cmd = (
        "curl -sk -u \"%s:%s\" -H \"Content-Type: application/json\" %s"
        % (
            utils.DEFAULT_BIGIP_USERNAME,
            utils.DEFAULT_BIGIP_PASSWORD,
            vs_url,
        )
    )
    availability_msg = "The virtual server is available"

    def is_available():
        res = ssh.run(symbols.bastion, curl_cmd)
        return availability_msg in res

    while not is_available() and duration < timeout:
        time.sleep(interval)
        duration += interval
    time.sleep(interval)
    assert is_available()


def _verify_bigip_controller(kwargs):
    orchestration = kwargs['orchestration']
    ssh = kwargs['ssh']
    svc = orchestration.app.get(kwargs['svc_name'])
    svc_url = _get_svc_url(svc)
    actual_responses = []
    num_requests = 20
    curl_cmd = "curl -sk %s" % svc_url
    ptn = re.compile("^Hello from .+ :0\)$")
    for _ in range(num_requests):
        res = ssh.run(symbols.bastion, curl_cmd)
        # - verify response looks good
        assert re.match(ptn, res)
        if res not in actual_responses:
            actual_responses.append(res)
    # - verify we got responses from at least two different pool members
    assert len(actual_responses) >= 2


def _get_scale_config(kwargs):
    if symbols.orchestration == "marathon":
        cfg = {
            'F5_PARTITION': utils.DEFAULT_F5MLB_PARTITION,
            'F5_0_BIND_ADDR': utils.DEFAULT_F5MLB_BIND_ADDR,
            'F5_0_PORT': SVC_START_PORT + kwargs['idx'],
            'F5_0_MODE': utils.DEFAULT_F5MLB_MODE,
        }
    elif utils.is_kubernetes():
        cfg = deepcopy(utils.DEFAULT_SVC_CONFIG)
        vs = cfg['data']['data']['virtualServer']
        vs['frontend']['virtualAddress']['port'] += kwargs['idx']
    return cfg


def _get_svc_url(svc):
    if symbols.orchestration == "marathon":
        return (
            "http://%s:%s"
            % (svc.labels['F5_0_BIND_ADDR'], svc.labels['F5_0_PORT'])
        )
    if utils.is_kubernetes():
        vs_addr = svc.vs_config.get('frontend', {}).get('virtualAddress', {})
        return (
            "http://%s:%s"
            % (vs_addr['bindAddr'], vs_addr['port'])
        )
