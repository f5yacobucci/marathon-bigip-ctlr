"""BIG-IP Configuration Manager for the Cloud.

The CloudBigIP class (derived from f5.bigip) manages the state of a BIG-IP
based upon changes in the state of apps and tasks in Marathon; or services,
nodes, and pods in Kubernetes.

CloudBigIP manages the following BIG-IP resources:

    * Virtual Servers
    * Virtual Addresses
    * Pools
    * Pool Members
    * Nodes
    * Health Monitors
    * Application Services
"""

import ipaddress
import logging
import json
import requests
import f5
from operator import attrgetter
from common import resolve_ip, list_diff, list_intersect
from f5.bigip import BigIP
import icontrol.session
from requests.packages.urllib3.exceptions import InsecureRequestWarning

logger = logging.getLogger('marathon_lb')
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# common


def log_sequence(prefix, sequence_to_log):
    """Helper function to log a sequence.

    Dump a sequence to the logger, skip if it is empty

    Args:
        prefix: The prefix string to describe what's being logged
        sequence_to_log: The sequence being logged
    """
    if sequence_to_log:
        logger.debug(prefix + ': %s', (', '.join(sequence_to_log)))


def healthcheck_timeout_calculate(data):
    """Calculate a BIG-IP Health Monitor timeout.

    Args:
        data: BIG-IP config dict
    """
    # Calculate timeout
    # See the f5 monitor docs for explanation of settings:
    # https://goo.gl/JJWUIg
    # Formula to match up marathon settings with f5 settings:
    # (( maxConsecutiveFailures - 1) * intervalSeconds )
    # + timeoutSeconds + 1
    timeout = (
         ((data['maxConsecutiveFailures'] - 1) * data['intervalSeconds']) +
         data['timeoutSeconds'] + 1
    )
    return timeout


def get_protocol(protocol):
    """Return the protocol (tcp or udp)."""
    if str(protocol).lower() == 'tcp':
        return 'tcp'
    if str(protocol).lower() == 'http':
        return 'tcp'
    if str(protocol).lower() == 'udp':
        return 'udp'
    else:
        return None


def has_partition(partitions, app_partition):
    """Check if the app_partition is one we're responsible for."""
    # App has no partition specified
    if not app_partition:
        return False

    # All partitions / wildcard match
    if '*' in partitions:
        return True

    # empty partition only
    if len(partitions) == 0 and not app_partition:
        raise Exception("No partitions specified")

    # Contains matching partitions
    if app_partition in partitions:
        return True

    return False


class CloudBigIP(BigIP):
    """CloudBigIP class.

    Generates a configuration for a BigIP based upon the apps/tasks managed
    by Marathon or services/pods/nodes in Kubernetes.

    - Matches apps/sevices by BigIP partition
    - Creates a Virtual Server and pool for each service type that matches a
      BigIP partition
    - For each backend (task, node, or pod), it creates a pool member and adds
      the member to the pool
    - If the app has a Marathon Health Monitor configured, create a
      corresponding health monitor for the BigIP pool member

    Args:
        cloud: cloud environment (marathon or kubernetes)
        hostname: IP address of BIG-IP
        username: BIG-IP username
        password: BIG-IP password
        partitions: List of BIG-IP partitions to manage
    """

    def __init__(self, cloud, hostname, port, username, password, partitions):
        """Initialize the CloudBigIP object."""
        super(CloudBigIP, self).__init__(hostname, username, password,
                                         port=port)
        self._cloud = cloud
        self._hostname = hostname
        self._port = port
        self._username = username
        self._password = password
        self._partitions = partitions
        self._lbmethods = (
            "dynamic-ratio-member",
            "least-connections-member",
            "observed-node",
            "ratio-least-connections-node",
            "round-robin",
            "dynamic-ratio-node",
            "least-connections-node",
            "predictive-member",
            "ratio-member",
            "weighted-least-connections-member",
            "fastest-app-response",
            "least-sessions",
            "predictive-node",
            "ratio-node",
            "weighted-least-connections-node",
            "fastest-node",
            "observed-member",
            "ratio-least-connections-member",
            "ratio-session"
            )

    def is_label_data_valid(self, app):
        """Validate the Marathon app's label data.

        Args:
            app: The app to be validated
        """
        is_valid = True
        msg = 'Application label {0} for {1} contains an invalid value: {2}'

        # Validate mode
        if get_protocol(app.mode) is None:
            logger.error(msg.format('F5_MODE', app.appId, app.mode))
            is_valid = False

        # Validate port
        if app.servicePort < 1 or app.servicePort > 65535:
            logger.error(msg.format('F5_PORT', app.appId, app.servicePort))
            is_valid = False

        # Validate address
        try:
            ipaddress.ip_address(app.bindAddr)
        except ValueError:
            logger.error(msg.format('F5_BIND_ADDR', app.appId, app.bindAddr))
            is_valid = False

        # Validate LB method
        if app.balance not in self._lbmethods:
            logger.error(msg.format('F5_BALANCE', app.appId, app.balance))
            is_valid = False

        return is_valid

    def regenerate_config_f5(self, cloud_state):
        """Configure the BIG-IP based on the cloud state.

        Args:
            cloud_state: Marathon or Kubernetes state
        """
        try:
            if self._cloud == 'marathon':
                cfg = self._create_config_marathon(cloud_state)
            else:
                if hasattr(cloud_state, 'items') and 'services' in cloud_state:
                    # New format: full config
                    services = cloud_state['services']
                else:
                    # Old format: list of services
                    services = cloud_state
                cfg = self._create_config_kubernetes(services)
            self._apply_config(cfg)

        # Handle F5/BIG-IP exceptions here
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error: {}".format(e))
            # Indicate that we need to retry
            return True
        except f5.sdk_exception.F5SDKError as e:
            logger.error("Resource Error: {}".format(e))
            # Indicate that we need to retry
            return True
        except icontrol.exceptions.BigIPInvalidURL as e:
            logger.error("Invalid URL: {}".format(e))
            # Indicate that we need to retry
            return True
        except icontrol.exceptions.iControlUnexpectedHTTPError as e:
            logger.error("HTTP Error: {}".format(e))
            # Indicate that we need to retry
            return True
        except Exception as e:
            raise

        return False

    def _create_config_kubernetes(self, svcs):
        """Create a BIG-IP configuration from the Kubernetes svc list.

        Args:
            svcs: Kubernetes svc list
        """
        logger.info("Generating config for BIG-IP from Kubernetes state")
        f5 = {}

        # partitions this script is responsible for:
        partitions = frozenset(self._partitions)

        for svc in svcs:
            f5_service = {}

            backend = svc['virtualServer']['backend']
            frontend = svc['virtualServer']['frontend']
            health_monitors = backend.get('healthMonitors', [])

            # Only handle application if it's partition is one that this script
            # is responsible for
            if not has_partition(partitions, frontend['partition']):
                continue

            # No address for this port
            if (('virtualAddress' not in frontend or
                 'bindAddr' not in frontend['virtualAddress']) and
                    'iapp' not in frontend):
                continue

            virt_addr = ('iapp' if 'iapp' in frontend else
                         frontend['virtualAddress']['bindAddr'])
            port = (backend['servicePort'] if 'virtualAddress' not in frontend
                    else frontend['virtualAddress']['port'])
            frontend_name = "{0}_{1}_{2}".format(
                backend['serviceName'].strip('/'), virt_addr, port)

            f5_service['name'] = frontend_name

            f5_service['partition'] = frontend['partition']

            if 'iapp' in frontend:
                f5_service['iapp'] = {'template': frontend['iapp'],
                                      'tableName': frontend['iappTableName'],
                                      'variables': frontend['iappVariables'],
                                      'options': frontend['iappOptions']}
            else:
                f5_service['virtual'] = {}
                f5_service['pool'] = {}
                f5_service['health'] = []

                # Parse the SSL profile into partition and name
                profiles = []
                if 'sslProfile' in frontend:
                    profile = (
                        frontend['sslProfile']['f5ProfileName'].split('/'))
                    if len(profile) != 2:
                        logger.error("Could not parse partition and name from "
                                     "SSL profile: %s",
                                     frontend['sslProfile']['f5ProfileName'])
                    else:
                        profiles.append({'partition': profile[0],
                                         'name': profile[1]})

                # Add appropriate profiles
                if str(frontend['mode']).lower() == 'http':
                    profiles.append({'partition': 'Common', 'name': 'http'})
                elif get_protocol(frontend['mode']) == 'tcp':
                    profiles.append({'partition': 'Common', 'name': 'tcp'})

                f5_service['virtual_address'] = frontend['virtualAddress'][
                    'bindAddr']

                f5_service['virtual'].update({
                    'enabled': True,
                    'disabled': False,
                    'ipProtocol': get_protocol(frontend['mode']),
                    'destination':
                    "/%s/%s:%d" % (frontend['partition'],
                                   frontend['virtualAddress']['bindAddr'],
                                   frontend['virtualAddress']['port']),
                    'pool': "/%s/%s" % (frontend['partition'], frontend_name),
                    'sourceAddressTranslation': {'type': 'automap'},
                    'profiles': profiles
                })

                monitors = None
                # Health Monitors
                for index, health in enumerate(health_monitors):
                    logger.debug("Healthcheck for service %s: %s",
                                 backend['serviceName'], health)
                    if index == 0:
                        health['name'] = frontend_name
                    else:
                        health['name'] = frontend_name + '_' + str(index)
                        monitors = monitors + ' and '
                    f5_service['health'].append(health)

                    # monitors is a string of health-monitor names
                    # delimited by ' and '
                    monitor = "/%s/%s" % (frontend['partition'],
                                          f5_service['health'][index]['name'])

                    monitors = (monitors + monitor) if monitors is not None \
                        else monitor

                f5_service['pool'].update({
                    'monitor': monitors,
                    'loadBalancingMode': frontend['balance']
                })

            f5_service['nodes'] = {}
            poolMemberPort = backend['poolMemberPort']
            for node in backend['poolMemberAddrs']:
                f5_node_name = node + ':' + str(poolMemberPort)
                f5_service['nodes'].update({f5_node_name: {
                    'state': 'user-up',
                    'session': 'user-enabled'
                }})

            f5.update({frontend_name: f5_service})

        return f5

    def _create_config_marathon(self, apps):
        """Create a BIG-IP configuration from the Marathon app list.

        Args:
            apps: Marathon app list
        """
        logger.debug(apps)
        for app in apps:
            logger.debug(app.__hash__())

        logger.info("Generating config for BIG-IP")
        f5 = {}
        # partitions this script is responsible for:
        partitions = frozenset(self._partitions)

        for app in sorted(apps, key=attrgetter('appId', 'servicePort')):
            f5_service = {
                'virtual': {},
                'pool': {},
                'nodes': {},
                'health': [],
                'partition': '',
                'name': ''
            }
            # Only handle application if it's partition is one that this script
            # is responsible for
            if not has_partition(partitions, app.partition):
                continue

            # No address or iApp for this port
            if not app.bindAddr and not app.iapp:
                continue

            # Validate data from the app's labels
            if not app.iapp and not self.is_label_data_valid(app):
                continue

            f5_service['partition'] = app.partition

            if app.iapp:
                f5_service['iapp'] = {'template': app.iapp,
                                      'tableName': app.iappTableName,
                                      'variables': app.iappVariables,
                                      'options': app.iappOptions}

            logger.info("Configuring app %s, partition %s", app.appId,
                        app.partition)
            backend = app.appId[1:].replace('/', '_') + '_' + \
                str(app.servicePort)

            frontend = 'iapp' if app.iapp else app.bindAddr
            frontend_name = "%s_%s_%d" % ((app.appId).lstrip('/'), frontend,
                                          app.servicePort)
            f5_service['name'] = frontend_name
            if app.bindAddr:
                logger.debug("Frontend at %s:%d with backend %s", app.bindAddr,
                             app.servicePort, backend)

            if app.healthCheck:
                logger.debug("Healthcheck for app '%s': %s", app.appId,
                             app.healthCheck)
                app.healthCheck['name'] = frontend_name

                # normalize healtcheck protocol name to lowercase
                if 'protocol' in app.healthCheck:
                    app.healthCheck['protocol'] = \
                        (app.healthCheck['protocol']).lower()
                app.healthCheck.update({
                    'interval': app.healthCheck['intervalSeconds']
                    if app.healthCheck else None,
                    'timeout': healthcheck_timeout_calculate(app.healthCheck)
                    if app.healthCheck else None,
                    'send': self.healthcheck_sendstring(app.healthCheck)
                    if app.healthCheck else None
                })
                f5_service['health'].append(app.healthCheck)

            # Parse the SSL profile into partition and name
            profiles = []
            if app.profile:
                profile = app.profile.split('/')
                if len(profile) != 2:
                    logger.error("Could not parse partition and name from SSL"
                                 " profile: %s", app.profile)
                else:
                    profiles.append({'partition': profile[0],
                                     'name': profile[1]})

            # Add appropriate profiles
            if str(app.mode).lower() == 'http':
                profiles.append({'partition': 'Common', 'name': 'http'})
            elif get_protocol(app.mode) == 'tcp':
                profiles.append({'partition': 'Common', 'name': 'tcp'})

            f5_service['virtual_address'] = app.bindAddr

            f5_service['virtual'].update({
                'enabled': True,
                'disabled': False,
                'ipProtocol': get_protocol(app.mode),
                'destination':
                "/%s/%s:%d" % (app.partition, app.bindAddr, app.servicePort),
                'pool': "/%s/%s" % (app.partition, frontend_name),
                'sourceAddressTranslation': {'type': 'automap'},
                'profiles': profiles
            })
            f5_service['pool'].update({
                'monitor': "/%s/%s" %
                           (app.partition, f5_service['health'][0]['name'])
                if app.healthCheck else None,
                'loadBalancingMode': app.balance
            })

            key_func = attrgetter('host', 'port')
            for backendServer in sorted(app.backends, key=key_func):
                logger.debug("Found backend server at %s:%d for app %s",
                             backendServer.host, backendServer.port, app.appId)

                # Resolve backendServer hostname to IP address
                ipv4 = resolve_ip(backendServer.host)

                if ipv4 is not None:
                    f5_node_name = ipv4 + ':' + str(backendServer.port)
                    f5_service['nodes'].update({f5_node_name: {
                        'state': 'user-up',
                        'session': 'user-enabled'
                    }})
                else:
                    logger.warning("Could not resolve ip for host %s, "
                                   "ignoring this backend", backendServer.host)

            f5.update({frontend_name: f5_service})

        logger.debug("F5 json config: %s", json.dumps(f5))

        return f5

    def _apply_config(self, config):
        """Apply the configuration to the BIG-IP.

        Args:
            config: BIG-IP config dict
        """
        unique_partitions = self.get_partitions(self._partitions)

        for partition in unique_partitions:
            logger.debug("Doing config for partition '%s'" % partition)

            marathon_virtual_list = \
                [x for x in config.keys()
                 if config[x]['partition'] == partition and
                 'iapp' not in config[x]]
            marathon_pool_list = \
                [x for x in config.keys()
                 if config[x]['partition'] == partition and
                 'iapp' not in config[x]]
            marathon_iapp_list = \
                [x for x in config.keys()
                 if config[x]['partition'] == partition and
                 'iapp' in config[x]]

            # Configure iApps
            f5_iapp_list = self.get_iapp_list(partition)
            log_sequence('f5_iapp_list', f5_iapp_list)
            log_sequence('marathon_iapp_list', marathon_iapp_list)

            # iapp delete
            iapp_delete = list_diff(f5_iapp_list, marathon_iapp_list)
            log_sequence('iApps to delete', iapp_delete)
            for iapp in iapp_delete:
                self.iapp_delete(partition, iapp)

            # iapp add
            iapp_add = list_diff(marathon_iapp_list, f5_iapp_list)
            log_sequence('iApps to add', iapp_add)
            for iapp in iapp_add:
                self.iapp_create(partition, iapp, config[iapp])

            # iapp update
            iapp_intersect = list_intersect(marathon_iapp_list, f5_iapp_list)
            log_sequence('iApps to update', iapp_intersect)
            for iapp in iapp_intersect:
                self.iapp_update(partition, iapp, config[iapp])

            # this is kinda kludgey: health monitor has the same name as the
            # virtual, and there is no more than 1 monitor per virtual.
            marathon_healthcheck_list = []
            for v in marathon_virtual_list:
                for hc in config[v]['health']:
                    if 'protocol' in hc:
                        marathon_healthcheck_list.append(v)

            f5_pool_list = self.get_pool_list(partition)
            f5_virtual_list = self.get_virtual_list(partition)

            # get_healthcheck_list() returns a dict with healthcheck names for
            # keys and a subkey of "type" with a value of "tcp", "http", etc.
            # We need to know the type to correctly reference the resource.
            # i.e. monitor types are different resources in the f5-sdk
            f5_healthcheck_dict = self.get_healthcheck_list(partition)
            logger.debug("f5_healthcheck_dict:   %s", f5_healthcheck_dict)
            # and then we need just the list to identify differences from the
            # list returned from marathon
            f5_healthcheck_list = f5_healthcheck_dict.keys()

            # The virtual servers, pools, and health monitors for iApps are
            # managed by the iApps themselves, so remove them from the lists we
            # manage
            for iapp in marathon_iapp_list:
                f5_virtual_list = \
                    [x for x in f5_virtual_list if not x.startswith(iapp)]
                f5_pool_list = \
                    [x for x in f5_pool_list if not x.startswith(iapp)]
                f5_healthcheck_list = \
                    [x for x in f5_healthcheck_list if not x.startswith(iapp)]

            log_sequence('f5_pool_list', f5_pool_list)
            log_sequence('f5_virtual_list', f5_virtual_list)
            log_sequence('f5_healthcheck_list', f5_healthcheck_list)
            log_sequence('marathon_pool_list', marathon_pool_list)
            log_sequence('marathon_virtual_list', marathon_virtual_list)

            # virtual delete
            virt_delete = list_diff(f5_virtual_list, marathon_virtual_list)
            log_sequence('Virtual Servers to delete', virt_delete)
            for virt in virt_delete:
                self.virtual_delete(partition, virt)

            # pool delete
            pool_delete_list = list_diff(f5_pool_list, marathon_pool_list)
            log_sequence('Pools to delete', pool_delete_list)
            for pool in pool_delete_list:
                self.pool_delete(partition, pool)

            # healthcheck delete
            health_delete = list_diff(f5_healthcheck_list,
                                      marathon_healthcheck_list)
            log_sequence('Healthchecks to delete', health_delete)
            for hc in health_delete:
                self.healthcheck_delete(partition, hc,
                                        f5_healthcheck_dict[hc]['type'])

            # healthcheck config needs to happen before pool config because
            # the pool is where we add the healthcheck
            # healthcheck add: use the name of the virt for the healthcheck
            healthcheck_add = list_diff(marathon_healthcheck_list,
                                        f5_healthcheck_list)
            log_sequence('Healthchecks to add', healthcheck_add)
            for hc in healthcheck_add:
                for item in config[hc]['health']:
                    self.healthcheck_create(partition, item)

            # pool add
            pool_add = list_diff(marathon_pool_list, f5_pool_list)
            log_sequence('Pools to add', pool_add)
            for pool in pool_add:
                self.pool_create(partition, pool, config[pool])

            # virtual add
            virt_add = list_diff(marathon_virtual_list, f5_virtual_list)
            log_sequence('Virtual Servers to add', virt_add)
            for virt in virt_add:
                self.virtual_create(partition, virt, config[virt])

            # healthcheck intersection
            healthcheck_intersect = list_intersect(marathon_virtual_list,
                                                   f5_healthcheck_list)
            log_sequence('Healthchecks to update', healthcheck_intersect)

            for hc in healthcheck_intersect:
                for item in config[hc]['health']:
                    self.healthcheck_update(partition, hc, item)

            # pool intersection
            pool_intersect = list_intersect(marathon_pool_list, f5_pool_list)
            log_sequence('Pools to update', pool_intersect)
            for pool in pool_intersect:
                self.pool_update(partition, pool, config[pool])

            # virt intersection
            virt_intersect = list_intersect(marathon_virtual_list,
                                            f5_virtual_list)
            log_sequence('Virtual Servers to update', virt_intersect)

            for virt in virt_intersect:
                self.virtual_update(partition, virt, config[virt])

            # add/update/remove pool members
            # need to iterate over pool_add and pool_intersect (note that
            # removing a pool also removes members, so don't have to
            # worry about those)
            for pool in list(set(pool_add + pool_intersect)):
                logger.debug("Pool: %s", pool)

                f5_member_list = self.get_pool_member_list(partition, pool)
                marathon_member_list = (config[pool]['nodes']).keys()

                member_delete_list = list_diff(f5_member_list,
                                               marathon_member_list)
                log_sequence('Pool members to delete', member_delete_list)
                for member in member_delete_list:
                    self.member_delete(partition, pool, member)

                member_add = list_diff(marathon_member_list, f5_member_list)
                log_sequence('Pool members to add', member_add)
                for member in member_add:
                    self.member_create(partition, pool, member,
                                       config[pool]['nodes'][member])

                # Since we're only specifying hostname and port for members,
                # 'member_update' will never actually get called. Changing
                # either of these properties will result in a new member being
                # created and the old one being deleted. I'm leaving this here
                # though in case we add other properties to members
                member_update_list = list_intersect(marathon_member_list,
                                                    f5_member_list)
                log_sequence('Pool members to update', member_update_list)

                for member in member_update_list:
                    self.member_update(partition, pool, member,
                                       config[pool]['nodes'][member])

            # Delete any unreferenced nodes
            self.cleanup_nodes(partition)

    def cleanup_nodes(self, partition):
        """Delete any unused nodes in a partition from the BIG-IP.

        Args:
            partition: Partition name
        """
        node_list = self.get_node_list(partition)
        pool_list = self.get_pool_list(partition)

        # Search pool members for nodes still in-use, if the node is still
        # being used, remove it from the node list
        for pool in pool_list:
            member_list = self.get_pool_member_list(partition, pool)
            for member in member_list:
                name, port = member.split(':')
                if name in node_list:
                    # Still in-use
                    node_list.remove(name)

                    node = self.get_node(name=name, partition=partition)
                    data = {'state': 'user-up', 'session': 'user-enabled'}

                    # Node state will be 'up' if it has a monitor attached,
                    # and 'unchecked' for no monitor
                    if node.state == 'up' or node.state == 'unchecked':
                        if 'enabled' in node.session:
                            continue

                    node.modify(**data)

        # What's left in the node list is not referenced, delete
        for node in node_list:
            self.node_delete(node, partition)

    def node_delete(self, node_name, partition):
        """Delete a node from the BIG-IP partition.

        Args:
            node_name: Node name
            partition: Partition name
        """
        node = self.ltm.nodes.node.load(name=node_name, partition=partition)
        node.delete()

    def get_pool(self, partition, name):
        """Get a pool object.

        Args:
            partition: Partition name
            name: Pool name
        """
        # return pool object

        # TODO: This is the efficient way to lookup a pool object:
        #
        #       p = self.ltm.pools.pool.load(
        #           name=name,
        #           partition=partition
        #       )
        #       return p
        #
        # However, this doesn't work if the path to the pool contains a
        # subpath. This is a known problem in the F5 SDK:
        #     https://github.com/F5Networks/f5-common-python/issues/468
        #
        # The alternative (below) is to get the collection of pool objects
        # and then search the list for the matching pool name.

        pools = self.ltm.pools.get_collection()
        for pool in pools:
            if pool.name == name:
                return pool

        return None

    def get_pool_list(self, partition):
        """Get a list of pool names for a partition.

        Args:
            partition: Partition name
        """
        pool_list = []
        pools = self.ltm.pools.get_collection()
        for pool in pools:
            if pool.partition == partition:
                pool_list.append(pool.name)
        return pool_list

    def pool_create(self, partition, pool, data):
        """Create a pool.

        Args:
            partition: Partition name
            pool: Name of pool to create
            data: BIG-IP config dict
        """
        logger.debug("Creating pool %s", pool)
        p = self.ltm.pools.pool

        p.create(partition=partition, name=pool, **data['pool'])

    def pool_delete(self, partition, pool):
        """Delete a pool.

        Args:
            partition: Partition name
            pool: Name of pool to delete
        """
        logger.debug("deleting pool %s", pool)
        p = self.get_pool(partition, pool)
        p.delete()

    def pool_update(self, partition, pool, data):
        """Update a pool.

        Args:
            partition: Partition name
            pool: Name of pool to update
            data: BIG-IP config dict
        """
        data = data['pool']
        pool = self.get_pool(partition, pool)

        def genChange(p, d):
            for key, val in p.__dict__.iteritems():
                if key in d:
                    if None is not val:
                        yield d[key] == val.strip()
                    else:
                        yield d[key] == val

        no_change = all(genChange(pool, data))

        if no_change:
            return False

        pool.modify(**data)
        return True

    def get_member(self, partition, pool, member):
        """Get a pool-member object.

        Args:
            partition: Partition name
            pool: Name of pool
            member: Name of pool member
        """
        p = self.get_pool(partition, pool)
        m = p.members_s.members.load(name=member, partition=partition)
        return m

    def get_pool_member_list(self, partition, pool):
        """Get a list of pool-member names.

        Args:
            partition: Partition name
            pool: Name of pool
        """
        member_list = []
        p = self.get_pool(partition, pool)
        members = p.members_s.get_collection()
        for member in members:
            member_list.append(member.name)

        return member_list

    def member_create(self, partition, pool, member, data):
        """Create a pool member.

        Args:
            partition: Partition name
            pool: Name of pool
            member: Name of pool member
            data: BIG-IP config dict
        """
        p = self.get_pool(partition, pool)
        member = p.members_s.members.create(
            name=member, partition=partition, **data)

    def member_delete(self, partition, pool, member):
        """Delete a pool member.

        Args:
            partition: Partition name
            pool: Name of pool
            member: Name of pool member
        """
        member = self.get_member(partition, pool, member)
        member.delete()

    def member_update(self, partition, pool, member, data):
        """Update a pool member.

        Args:
            partition: Partition name
            pool: Name of pool
            member: Name of pool member
            data: BIG-IP config dict
        """
        member = self.get_member(partition, pool, member)

        # Member state will be 'up' if it has a monitor attached,
        # and 'unchecked' for no monitor
        if member.state == 'up' or member.state == 'unchecked':
            if 'enabled' in member.session:
                return False

        member.modify(**data)
        return True

    def get_node(self, partition, name):
        """Get a node object.

        Args:
            partition: Partition name
            name: Name of the node
        """
        if self.ltm.nodes.node.exists(name=name, partition=partition):
            return self.ltm.nodes.node.load(name=name, partition=partition)
        else:
            return None

    def get_node_list(self, partition):
        """Get a list of node names for a partition.

        Args:
            partition: Partition name
        """
        node_list = []
        nodes = self.ltm.nodes.get_collection()
        for node in nodes:
            if node.partition == partition:
                node_list.append(node.name)

        return node_list

    def get_virtual(self, partition, virtual):
        """Get Virtual Server object.

        Args:
            partition: Partition name
            virtual: Name of the Virtual Server
        """
        # return virtual object
        v = self.ltm.virtuals.virtual.load(name=virtual, partition=partition)
        return v

    def get_virtual_list(self, partition):
        """Get a list of virtual-server names for a partition.

        Args:
            partition: Partition name
        """
        virtual_list = []
        virtuals = self.ltm.virtuals.get_collection()
        for virtual in virtuals:
            if virtual.partition == partition:
                virtual_list.append(virtual.name)

        return virtual_list

    def virtual_create(self, partition, virtual, data):
        """Create a Virtual Server.

        Args:
            partition: Partition name
            virtual: Name of the virtual server
            data: BIG-IP config dict
        """
        logger.debug("Creating Virtual Server %s", virtual)
        data = data['virtual']
        v = self.ltm.virtuals.virtual

        v.create(name=virtual, partition=partition, **data)

    def virtual_delete(self, partition, virtual):
        """Delete a Virtual Server.

        Args:
            partition: Partition name
            virtual: Name of the Virtual Server
        """
        logger.debug("Deleting Virtual Server %s", virtual)
        v = self.get_virtual(partition, virtual)
        v.delete()

    def virtual_update(self, partition, virtual, data):
        """Update a Virtual Server.

        Args:
            partition: Partition name
            virtual: Name of the Virtual Server
            data: BIG-IP config dict
        """
        addr = data['virtual_address']

        # Verify virtual address, recreate it if it doesn't exist
        v_addr = self.get_virtual_address(partition, addr)

        if v_addr is None:
            self.virtual_address_create(partition, addr)
        else:
            self.virtual_address_update(v_addr)

        # Verify Virtual Server
        data = data['virtual']

        v = self.get_virtual(partition, virtual)

        no_change = all(data[key] == val for key, val in v.__dict__.iteritems()
                        if key in data)

        # Compare the actual and desired profiles
        profiles = self.get_virtual_profiles(v)
        no_profile_change = sorted(profiles) == sorted(data['profiles'])

        if no_change and no_profile_change:
            return False

        v.modify(**data)

        return True

    def get_virtual_profiles(self, virtual):
        """Get list of Virtual Server profiles from Virtual Server.

        Args:
            virtual: Virtual Server object
        """
        v_profiles = virtual.profiles_s.get_collection()
        profiles = []
        for profile in v_profiles:
            profiles.append({'name': profile.name,
                             'partition': profile.partition})

        return profiles

    def get_virtual_address(self, partition, name):
        """Get Virtual Address object.

        Args:
            partition: Partition name
            name: Name of the Virtual Address
        """
        if not self.ltm.virtual_address_s.virtual_address.exists(
                name=name, partition=partition):
            return None
        else:
            return self.ltm.virtual_address_s.virtual_address.load(
                name=name, partition=partition)

    def virtual_address_create(self, partition, name):
        """Create a Virtual Address.

        Args:
            partition: Partition name
            name: Name of the virtual address
        """
        self.ltm.virtual_address_s.virtual_address.create(
            name=name, partition=partition)

    def virtual_address_update(self, virtual_address):
        """Update a Virtual Address.

        Args:
            virtual_address: Virtual Address object
        """
        if virtual_address.enabled == 'no':
            virtual_address.modify(enabled='yes')

    def get_healthcheck(self, partition, hc, hc_type):
        """Get a Health Monitor object.

        Args:
            partition: Partition name
            hc: Name of the Health Monitor
            hc_type: Health Monitor type
        """
        # return hc object
        if hc_type == 'http':
            hc = self.ltm.monitor.https.http.load(name=hc, partition=partition)
        elif hc_type == 'tcp':
            hc = self.ltm.monitor.tcps.tcp.load(name=hc, partition=partition)

        return hc

    def get_healthcheck_list(self, partition):
        """Get a dict of Health Monitors for a partition.

        Args:
            partition: Partition name
        """
        # will need to handle HTTP and TCP

        healthcheck_dict = {}

        # HTTP
        healthchecks = self.ltm.monitor.https.get_collection()
        for hc in healthchecks:
            if hc.partition == partition:
                healthcheck_dict.update({hc.name: {'type': 'http'}})

        # TCP
        healthchecks = self.ltm.monitor.tcps.get_collection()
        for hc in healthchecks:
            if hc.partition == partition:
                healthcheck_dict.update({hc.name: {'type': 'tcp'}})

        return healthcheck_dict

    def healthcheck_delete(self, partition, hc, hc_type):
        """Delete a Health Monitor.

        Args:
            partition: Partition name
            hc: Name of the Health Monitor
            hc_type: Health Monitor type
        """
        logger.debug("Deleting healthcheck %s", hc)
        hc = self.get_healthcheck(partition, hc, hc_type)
        hc.delete()

    def healthcheck_sendstring(self, data):
        """Return the 'send' string for a health monitor.

        Args:
            data: Health Monitor dict
        """
        if data['protocol'] == "http":
            send_string = 'GET / HTTP/1.0\\r\\n\\r\\n'
            if 'path' in data:
                send_string = 'GET %s HTTP/1.0\\r\\n\\r\\n' % data['path']
            return send_string
        else:
            return None

    def get_healthcheck_fields(self, data):
        """Return a new dict containing only supported health monitor data.

        Args:
            data: Health Monitor dict
        """
        if data['protocol'] == "http":
            send_keys = ('adaptive',
                         'adaptiveDivergenceType',
                         'adaptiveDivergenceValue',
                         'adaptiveLimit',
                         'adaptiveSamplingTimespan',
                         'appService',
                         'defaultsFrom',
                         'description',
                         'destination',
                         'interval',
                         'ipDscp',
                         'manualResume',
                         'name',
                         'tmPartition',
                         'password',
                         'recv',
                         'recvDisable',
                         'reverse',
                         'send',
                         'timeUntilUp',
                         'timeout',
                         'transparent',
                         'upInterval',
                         'username',
                         )
        elif data['protocol'] == "tcp":
            send_keys = ('adaptive',
                         'adaptiveDivergenceType',
                         'adaptiveDivergenceValue',
                         'adaptiveLimit',
                         'adaptiveSamplingTimespan',
                         'appService',
                         'defaultsFrom',
                         'description',
                         'destination',
                         'interval',
                         'ipDscp',
                         'manualResume',
                         'name',
                         'tmPartition',
                         'recv',
                         'recvDisable',
                         'reverse',
                         'send',
                         'timeUntilUp',
                         'timeout',
                         'transparent',
                         'upInterval',
                         )
        else:
            raise Exception(
                'Protocol {} is not supported.'.format(data['protocol']))

        send_data = {}
        for k in data:
            if k in send_keys:
                send_data[k] = data[k]
        return send_data

    def healthcheck_update(self, partition, hc, data):
        """Update a Health Monitor.

        Args:
            partition: Partition name
            hc: Name of the Health Monitor
            data: Health Monitor dict
        """
        logger.debug("Updating healthcheck %s", hc)
        # get healthcheck object
        hc = self.get_healthcheck(partition, hc, data['protocol'])
        send_data = self.get_healthcheck_fields(data)

        no_change = all(send_data[key] == val
                        for key, val in hc.__dict__.iteritems()
                        if key in send_data)

        if no_change:
            return False

        hc.modify(**send_data)
        return True

    def get_http_healthmonitor(self):
        """Get an object than can create a http health monitor."""
        h = self.ltm.monitor.https
        return h.http

    def get_tcp_healthmonitor(self):
        """Get an object than can create a tcp health monitor."""
        h = self.ltm.monitor.tcps
        return h.tcp

    def healthcheck_create(self, partition, data):
        """Create a Health Monitor.

        Args:
            partition: Partition name
            data: Health Monitor dict
        """
        send_data = self.get_healthcheck_fields(data)

        if data['protocol'] == "http":
            http1 = self.get_http_healthmonitor()
            http1.create(partition=partition, **send_data)

        if data['protocol'] == "tcp":
            tcp1 = self.get_tcp_healthmonitor()
            tcp1.create(partition=partition, **send_data)

    def get_partitions(self, partitions):
        """Get a list of BIG-IP partition names.

        Args:
            partitions: The list of partition names we're configured to manage
                        (Could be wildcard: '*')
        """
        if ('*' in partitions):
            # Wildcard means all partitions, so we need to query BIG-IP for the
            # actual partition names
            partition_list = []
            for folder in self.sys.folders.get_collection():
                if (not folder.name == "Common" and not folder.name == "/" and
                        not folder.name.endswith(".app")):

                    partition_list.append(folder.name)
            return partition_list
        else:
            # No wildcard, so we just care about those already configured
            return partitions

    def iapp_build_definition(self, config):
        """Create a dict that defines the 'variables' and 'tables' for an iApp.

        Args:
            config: BIG-IP config dict
        """
        # Build variable list
        variables = []
        for key in config['iapp']['variables']:
            var = {'name': key, 'value': config['iapp']['variables'][key]}
            variables.append(var)

        # Build table
        tables = [{'columnNames': ['addr', 'port', 'connection_limit'],
                   'name': config['iapp']['tableName'],
                   'rows': []
                   }]
        for node in config['nodes']:
            node = node.split(':')
            tables[0]['rows'].append({'row': [node[0], node[1], '0']})

        return {'variables': variables, 'tables': tables}

    def iapp_create(self, partition, name, config):
        """Create an iApp Application Service.

        Args:
            partition: Partition name
            name: Application Service name
            config: BIG-IP config dict
        """
        logger.debug("Creating iApp %s from template %s",
                     name, config['iapp']['template'])
        a = self.sys.application.services.service

        iapp_def = self.iapp_build_definition(config)

        a.create(
            name=name,
            template=config['iapp']['template'],
            partition=partition,
            variables=iapp_def['variables'],
            tables=iapp_def['tables'],
            **config['iapp']['options']
            )

    def iapp_delete(self, partition, name):
        """Delete an iApp Application Service.

        Args:
            partition: Partition name
            name: Application Service name
        """
        logger.debug("Deleting iApp %s", name)
        a = self.get_iapp(partition, name)
        a.delete()

    def iapp_update(self, partition, name, config):
        """Update an iApp Application Service.

        Args:
            partition: Partition name
            name: Application Service name
            config: BIG-IP config dict
        """
        a = self.get_iapp(partition, name)

        iapp_def = self.iapp_build_definition(config)

        a.update(
            executeAction='definition',
            name=name,
            partition=partition,
            variables=iapp_def['variables'],
            tables=iapp_def['tables'],
            **config['iapp']['options']
            )

    def get_iapp(self, partition, name):
        """Get an iApp Application Service object.

        Args:
            partition: Partition name
            name: Application Service name
        """
        a = self.sys.application.services.service.load(
                name=name,
                partition=partition
                )
        return a

    def get_iapp_list(self, partition):
        """Get a list of iApp Application Service names.

        Args:
            partition: Partition name
        """
        iapp_list = []
        iapps = self.sys.application.services.get_collection()
        for iapp in iapps:
            if iapp.partition == partition:
                iapp_list.append(iapp.name)

        return iapp_list
