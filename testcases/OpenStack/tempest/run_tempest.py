#!/usr/bin/env python
#
# Description:
#    Runs tempest and pushes the results to the DB
#
# Authors:
#    morgan.richomme@orange.com
#    jose.lausuch@ericsson.com
#    viktor.tikkanen@nokia.com
#
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the Apache License, Version 2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
#
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import yaml
import ConfigParser

import keystoneclient.v2_0.client as ksclient
from glanceclient import client as glanceclient
from neutronclient.v2_0 import client as neutronclient

import functest.utils.functest_logger as ft_logger
import functest.utils.functest_utils as ft_utils
import functest.utils.openstack_utils as os_utils

modes = ['full', 'smoke', 'baremetal', 'compute', 'data_processing',
         'identity', 'image', 'network', 'object_storage', 'orchestration',
         'telemetry', 'volume', 'custom', 'defcore']

""" tests configuration """
parser = argparse.ArgumentParser()
parser.add_argument("-d", "--debug",
                    help="Debug mode",
                    action="store_true")
parser.add_argument("-s", "--serial",
                    help="Run tests in one thread",
                    action="store_true")
parser.add_argument("-m", "--mode",
                    help="Tempest test mode [smoke, all]",
                    default="smoke")
parser.add_argument("-r", "--report",
                    help="Create json result file",
                    action="store_true")
parser.add_argument("-n", "--noclean",
                    help="Don't clean the created resources for this test.",
                    action="store_true")

args = parser.parse_args()

""" logging configuration """
logger = ft_logger.Logger("run_tempest").getLogger()

REPO_PATH = os.environ['repos_dir'] + '/functest/'


with open(os.environ["CONFIG_FUNCTEST_YAML"]) as f:
    functest_yaml = yaml.safe_load(f)
f.close()
TEST_DB = functest_yaml.get("results").get("test_db_url")

MODE = "smoke"
GLANCE_IMAGE_NAME = functest_yaml.get("general").get(
    "openstack").get("image_name")
GLANCE_IMAGE_FILENAME = functest_yaml.get("general").get(
    "openstack").get("image_file_name")
GLANCE_IMAGE_FORMAT = functest_yaml.get("general").get(
    "openstack").get("image_disk_format")
GLANCE_IMAGE_PATH = functest_yaml.get("general").get("directories").get(
    "dir_functest_data") + "/" + GLANCE_IMAGE_FILENAME
PRIVATE_NET_NAME = functest_yaml.get("tempest").get("private_net_name")
PRIVATE_SUBNET_NAME = functest_yaml.get("tempest").get("private_subnet_name")
PRIVATE_SUBNET_CIDR = functest_yaml.get("tempest").get("private_subnet_cidr")
ROUTER_NAME = functest_yaml.get("tempest").get("router_name")
TENANT_NAME = functest_yaml.get("tempest").get("identity").get("tenant_name")
TENANT_DESCRIPTION = functest_yaml.get("tempest").get("identity").get(
    "tenant_description")
USER_NAME = functest_yaml.get("tempest").get("identity").get("user_name")
USER_PASSWORD = functest_yaml.get("tempest").get("identity").get(
    "user_password")
DEPLOYMENT_MAME = functest_yaml.get("rally").get("deployment_name")
RALLY_INSTALLATION_DIR = functest_yaml.get("general").get("directories").get(
    "dir_rally_inst")
RESULTS_DIR = functest_yaml.get("general").get("directories").get(
    "dir_results")
TEMPEST_RESULTS_DIR = RESULTS_DIR + '/tempest'
TEST_LIST_DIR = functest_yaml.get("general").get("directories").get(
    "dir_tempest_cases")
TEMPEST_CUSTOM = REPO_PATH + TEST_LIST_DIR + 'test_list.txt'
TEMPEST_BLACKLIST = REPO_PATH + TEST_LIST_DIR + 'blacklist.txt'
TEMPEST_DEFCORE = REPO_PATH + TEST_LIST_DIR + 'defcore_req.txt'
TEMPEST_RAW_LIST = TEMPEST_RESULTS_DIR + '/test_raw_list.txt'
TEMPEST_LIST = TEMPEST_RESULTS_DIR + '/test_list.txt'


def get_info(file_result):
    test_run = ""
    duration = ""
    test_failed = ""

    p = subprocess.Popen('cat tempest.log',
                         shell=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    for line in p.stdout.readlines():
        # print line,
        if (len(test_run) < 1):
            test_run = re.findall("[0-9]*\.[0-9]*s", line)
        if (len(duration) < 1):
            duration = re.findall("[0-9]*\ tests", line)
        regexp = r"(failures=[0-9]+)"
        if (len(test_failed) < 1):
            test_failed = re.findall(regexp, line)

    logger.debug("test_run:" + test_run)
    logger.debug("duration:" + duration)


def create_tempest_resources():
    ks_creds = os_utils.get_credentials("keystone")
    logger.debug("Creating tenant and user for Tempest suite")
    keystone = ksclient.Client(**ks_creds)
    tenant_id = os_utils.create_tenant(keystone,
                                       TENANT_NAME,
                                       TENANT_DESCRIPTION)
    if tenant_id == '':
        logger.error("Error : Failed to create %s tenant" % TENANT_NAME)

    user_id = os_utils.create_user(keystone, USER_NAME, USER_PASSWORD,
                                   None, tenant_id)
    if user_id == '':
        logger.error("Error : Failed to create %s user" % USER_NAME)

    logger.debug("Creating private network for Tempest suite")
    creds_neutron = os_utils.get_credentials("neutron")
    neutron_client = neutronclient.Client(**creds_neutron)
    network_dic = os_utils.create_network_full(logger,
                                               neutron_client,
                                               PRIVATE_NET_NAME,
                                               PRIVATE_SUBNET_NAME,
                                               ROUTER_NAME,
                                               PRIVATE_SUBNET_CIDR)
    if network_dic:
        if not os_utils.update_neutron_net(neutron_client,
                                           network_dic['net_id'],
                                           shared=True):
            logger.error("Failed to update private network...")
            exit(-1)
        else:
            logger.debug("Network '%s' is available..." % PRIVATE_NET_NAME)
    else:
        logger.error("Private network creation failed")
        exit(-1)

    logger.debug("Creating image for Tempest suite")
    glance_endpoint = keystone.service_catalog.url_for(
        service_type='image', endpoint_type='publicURL')
    glance_client = glanceclient.Client(1, glance_endpoint,
                                        token=keystone.auth_token)
    # Check if the given image exists
    image_id = os_utils.get_image_id(glance_client, GLANCE_IMAGE_NAME)
    if image_id != '':
        logger.info("Using existing image '%s'..." % GLANCE_IMAGE_NAME)
    else:
        logger.info("Creating image '%s' from '%s'..." % (GLANCE_IMAGE_NAME,
                                                          GLANCE_IMAGE_PATH))
        image_id = os_utils.create_glance_image(glance_client,
                                                GLANCE_IMAGE_NAME,
                                                GLANCE_IMAGE_PATH)
        if not image_id:
            logger.error("Failed to create a Glance image...")
            exit(-1)
        logger.debug("Image '%s' with ID=%s created successfully."
                     % (GLANCE_IMAGE_NAME, image_id))


def configure_tempest(deployment_dir):
    """
    Add/update needed parameters into tempest.conf file generated by Rally
    """

    logger.debug("Generating tempest.conf file...")
    cmd = "rally verify genconfig"
    ft_utils.execute_command(cmd, logger)

    logger.debug("Finding tempest.conf file...")
    tempest_conf_file = deployment_dir + "/tempest.conf"
    if not os.path.isfile(tempest_conf_file):
        logger.error("Tempest configuration file %s NOT found."
                     % tempest_conf_file)
        exit(-1)

    logger.debug("Updating selected tempest.conf parameters...")
    config = ConfigParser.RawConfigParser()
    config.read(tempest_conf_file)
    config.set('compute', 'fixed_network_name', PRIVATE_NET_NAME)
    config.set('identity', 'tenant_name', TENANT_NAME)
    config.set('identity', 'username', USER_NAME)
    config.set('identity', 'password', USER_PASSWORD)
    with open(tempest_conf_file, 'wb') as config_file:
        config.write(config_file)

    # Copy tempest.conf to /home/opnfv/functest/results/tempest/
    shutil.copyfile(tempest_conf_file, TEMPEST_RESULTS_DIR + '/tempest.conf')
    return True


def read_file(filename):
    with open(filename) as src:
        return [line.strip() for line in src.readlines()]


def generate_test_list(deployment_dir, mode):
    logger.debug("Generating test case list...")
    if mode == 'defcore':
        shutil.copyfile(TEMPEST_DEFCORE, TEMPEST_RAW_LIST)
    elif mode == 'custom':
        if os.path.isfile(TEMPEST_CUSTOM):
            shutil.copyfile(TEMPEST_CUSTOM, TEMPEST_RAW_LIST)
        else:
            logger.error("Tempest test list file %s NOT found."
                         % TEMPEST_CUSTOM)
            exit(-1)
    else:
        if mode == 'smoke':
            testr_mode = "smoke"
        elif mode == 'full':
            testr_mode = ""
        else:
            testr_mode = 'tempest.api.' + mode
        cmd = ("cd " + deployment_dir + ";" + "testr list-tests " +
               testr_mode + ">" + TEMPEST_RAW_LIST + ";cd")
        ft_utils.execute_command(cmd, logger)


def apply_tempest_blacklist():
    logger.debug("Applying tempest blacklist...")
    cases_file = read_file(TEMPEST_RAW_LIST)
    result_file = open(TEMPEST_LIST, 'w')
    try:
        black_file = read_file(TEMPEST_BLACKLIST)
    except:
        black_file = ''
        logger.debug("Tempest blacklist file does not exist.")
    for line in cases_file:
        if line not in black_file:
            result_file.write(str(line) + '\n')
    result_file.close()


def run_tempest(OPTION):
    #
    # the "main" function of the script which launches Rally to run Tempest
    # :param option: tempest option (smoke, ..)
    # :return: void
    #
    logger.info("Starting Tempest test suite: '%s'." % OPTION)
    start_time = time.time()
    stop_time = start_time
    cmd_line = "rally verify start " + OPTION + " --system-wide"

    header = ("Tempest environment:\n"
              "  Installer: %s\n  Scenario: %s\n  Node: %s\n  Date: %s\n" %
              (os.getenv('INSTALLER_TYPE', 'Unknown'),
               os.getenv('DEPLOY_SCENARIO', 'Unknown'),
               os.getenv('NODE_NAME', 'Unknown'),
               time.strftime("%a %b %d %H:%M:%S %Z %Y")))

    f_stdout = open(TEMPEST_RESULTS_DIR + "/tempest.log", 'w+')
    f_stderr = open(TEMPEST_RESULTS_DIR + "/tempest-error.log", 'w+')
    f_env = open(TEMPEST_RESULTS_DIR + "/environment.log", 'w+')
    f_env.write(header)

    subprocess.call(cmd_line, shell=True, stdout=f_stdout, stderr=f_stderr)

    f_stdout.close()
    f_stderr.close()
    f_env.close()

    cmd_line = "rally verify show"
    ft_utils.execute_command(cmd_line, logger,
                             exit_on_error=True, info=True)

    cmd_line = "rally verify list"
    logger.debug('Executing command : {}'.format(cmd_line))
    cmd = os.popen(cmd_line)
    output = (((cmd.read()).splitlines()[-2]).replace(" ", "")).split("|")
    # Format:
    # | UUID | Deployment UUID | smoke | tests | failures | Created at |
    # Duration | Status  |
    num_tests = output[4]
    num_failures = output[5]
    time_start = output[6]
    duration = output[7]
    # Compute duration (lets assume it does not take more than 60 min)
    dur_min = int(duration.split(':')[1])
    dur_sec_float = float(duration.split(':')[2])
    dur_sec_int = int(round(dur_sec_float, 0))
    dur_sec_int = dur_sec_int + 60 * dur_min
    stop_time = time.time()
    # Push results in payload of testcase
    if args.report:
        logger.debug("Pushing tempest results into DB...")
        # Note criteria hardcoded...TODO move to testcase.yaml
        status = "FAIL"
        try:
            diff = (int(num_tests) - int(num_failures))
            success_rate = 100 * diff / int(num_tests)
        except:
            success_rate = 0

        # For Tempest we assume that the success rate is above 90%
        if success_rate >= 90:
            status = "PASS"

        # add the test in error in the details sections
        # should be possible to do it during the test
        with open(TEMPEST_RESULTS_DIR + "/tempest.log", 'r') as myfile:
            output = myfile.read()
        error_logs = ""

        for match in re.findall('(.*?)[. ]*FAILED', output):
                error_logs += match

        # Generate json results for DB
        json_results = {"timestart": time_start, "duration": dur_sec_int,
                        "tests": int(num_tests), "failures": int(num_failures),
                        "errors": error_logs}
        logger.info("Results: " + str(json_results))
        # split Tempest smoke and full
        if "smoke" in args.mode:
            case_name = "tempest_smoke_serial"
        else:
            case_name = "tempest_full_parallel"

        try:
            ft_utils.push_results_to_db("functest",
                                        case_name,
                                        None,
                                        start_time,
                                        stop_time,
                                        status,
                                        json_results)
        except:
            logger.error("Error pushing results into Database '%s'"
                         % sys.exc_info()[0])


def main():
    global MODE

    if not (args.mode in modes):
        logger.error("Tempest mode not valid. "
                     "Possible values are:\n" + str(modes))
        exit(-1)

    if not os.path.exists(TEMPEST_RESULTS_DIR):
        os.makedirs(TEMPEST_RESULTS_DIR)

    deployment_dir = ft_utils.get_deployment_dir(logger)
    configure_tempest(deployment_dir)
    create_tempest_resources()
    generate_test_list(deployment_dir, args.mode)
    apply_tempest_blacklist()

    MODE = "--tests-file " + TEMPEST_LIST
    if args.serial:
        MODE += " --concur 1"

    run_tempest(MODE)


if __name__ == '__main__':
    main()
