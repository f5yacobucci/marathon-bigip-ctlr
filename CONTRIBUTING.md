<!--
Copyright 2017 F5 Networks Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Contributing Guide for marathon-bigip-ctlr
If you have found this that means you you want to help us out. Thanks in advance for lending a hand! This guide should get you up and running quickly and make it easy for you to contribute.  

## Issues
We use issues for bug reports and to discuss new features. If you are planning on contributing a new feature, you should open an issue first that discusses the feature you're adding. This will avoid wasting your time if someone else is already working on it, or if there's some design change that we need.

Creating issues is good, creating good issues is even better. Filing meaningful bug reports with lots of information in them helps us figure out what to fix when and how it impacts our users. We like bugs because it means people are using our code, and we like fixing them even more.

Please follow these guidelines for filing issues.
* Describe the problem
* Include version of Mesos, Marathon, marathon-bigip-ctlr, and BIG-IP
* Include detailed information about how to recreate the issue
* Include relevant configuration, error messages and logs
* Sanitize the data. For example, be mindful of IPs, ports, application names and URLs

## Pull Requests
We use [travis-ci](https://travis-ci.org/F5Networks/marathon-bigip-ctlr) to automatically run some hooks on every pull request. These must pass before a pull request is accepted. You can see the details in [.travis.yml](https://github.com/F5Networks/marathon-bigip-ctlr/blob/master/.travis.yml). If your pull request fails in travis, your pull request will be blocked by travis with a link to the failing run. Generally, we run these hooks:
* Unit tests are executed
* Code coverage data is collected
* Formatting and linting checks are performed
* Third party attributions are generated and checked
* Product documentation is built
* Container is built
* Container is pushed to dockerhub (only after the pull request is accepted)

If you are submitting a pull request, you need to make sure that you have done a few things first.

* Make sure you have tested your code. Reviewers usually expect new unit tests for new code.
* The master branch must be kept release ready at all times. This requires that a single pull request should contain the code changes, unit tests, and documentation changes (if any)
* Use proper formatting for the code
* Clean up your git history because no one wants to see 75 commits for one issue
*  Use the commit message format shown below

## Commit Message Format
The commit message for your final commit should use the following format:
```
Fix #<issue-number>: <One line summarizing what changed>

Problem: Brief description of the problem.

Solution: Detailed description of your solution

Testing (optional if not described in Solution section): Description of tests that were run to exercise the solutions (unit tests, system tests, etc)

affects-branches: branch1, branch2
```
* The messages should be line-wrapped to 80 characters if possible. Try to keep the one line summary under 80 characters if possible.
* If a commit fixes many issues, list all of them
* A line stating what branches the pull request is going to be merged into is required. The note should follow the format "affects-branches: branch1, branch2". This is because we have a robot that can check if bugfixes have been appropriately backported. This is only needed for bugfixes, and if you don't know what to put here for a bug, ask in your pull request.


## Testing
Creating tests is straight forward and we need you to help us ensure the quality of our code. Every public API should have associated unit tests. We use [pylint](https://www.pylint.org/) and [flake8](http://flake8.pycqa.org/en/latest/) to verify code quality and the python [unittest](https://docs.python.org/2/library/unittest.html) module for unit testing. All of these can be invoked by simply running “make python-sanity”.

We have unit tests and functional tests. The functional tests require some harnessing to setup and run, and are not the responsibility of non-F5 contributors. But, some large pull requests may need coordination in advance because we need to make sure we have the functional test harnessing to support it.

## License

### Apache V2.0
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

### Contributor License Agreement
Individuals or business entities who contribute to this project must have completed and submitted the [F5® Contributor License Agreement](http://clouddocs.f5.com/containers/v1/cla_landing.html) to ContainerConnector_CLA@f5.com prior to their code submission being included in this project. Please include your github handle in the CLA email.