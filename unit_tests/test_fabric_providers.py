from nose.plugins.skip import SkipTest

__author__ = 'rpolli'
import logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
from docker import Client
from mysql.fabric.provider import Provider
from mysql.fabric.providers import dockerprovider
from mysql.fabric.providers.dockerprovider import (
    configure_provider,
    find_resource
)
from shlex import split

from mysql.fabric.providers import find_providers
find_providers()

created_containers = []
provider = Provider(provider_id='dockermock',
                    provider_type='DOCKER',
                    username='',
                    password='',
                    url=''
                    )


def teardown():
    for c in created_containers:
        log.warn("removing container associated to machine-: %r", c)
        cli = Client(provider.url)
        cid = cli.containers(filters={'name': 'machine-' + c})
        if not cid:
            log.warning("Container already removed")
            continue

        assert len(cid) == 1, "More than one container matching"

        try:
            cli.remove_container(cid[0]['Id'], v=True, force=True)
        except (IndexError, AttributeError) as e:
            log.error("Error in teardown %s", e)
            continue
"""
        parameters = {
            'image' : image,
            'flavor' : flavor,
            'number_machines' : number_machines,
            'availability_zone' : availability_zone,
            'key_name' : key_name,
            'security_groups' : security_groups,
            'private_network' : private_network,
            'public_network' : public_network,
            'userdata' : userdata,
            'swap' : swap,
            'block_device' : None,
            'scheduler_hints' : scheduler_hints,
            'private_nics' : None,
            'public_nics' : None,
            'meta' : meta,
            'datastore' : datastore,
            'datastore_version' : datastore_version,
            'size' : size,
            'databases' : databases,
            'users' : users,
            'configuration' : configuration,
            'security' : security,
        }

"""


def test_configure_provider():
    name, ManagerClass, _, pid = configure_provider()


def test_create_machine_manager():
    assert dockerprovider.MachineManager(provider)


def find_resource_test():
    # Retrieve image's reference.
    parameters = {
        'image': {'name': 'busybox'},
        'flavor': {'name': 'latest'},
        'meta': {
            'volumes': '/mnt/foo',
            'command': split('/bin/sleep 20')
        },
        'number_machines': 1,
    }
    c = Client()
    ret = find_resource(parameters['image'], c.images)
    log.warn(ret)
    assert ret


def test_create():
    wait_spawning = True
    parameters = {
        'image': {'name': 'busybox'},
        'flavor': {'name': 'latest'},
        'meta': {
            'volumes': '/mnt/foo',
            'command': split('/bin/sleep 20')
        },
        'number_machines': 1,

    }
    m = dockerprovider.MachineManager(provider, version='1.15')
    ret = m.create(parameters, wait_spawning)[0]
    log.warn(ret)
    created_containers.append(ret.uuid)
    assert ret


@SkipTest
def test_search_with_labels():
    # Search all Containers matching the given filter.
    #  if 'all': True searches all containers
    generic_filters = {'all': False, 'limit': 2}
    meta_filters = {
        'mysql-fabric-machine-group-uuid': 'e807df6a-0ae6-44cc-beaf-310d498598b4',
    }
    m = dockerprovider.MachineManager(provider, version='1.15')
    ret = m.search(generic_filters, meta_filters)
    assert ret

def test_search_generic():
    # Search all Containers matching the given filter.
    #  if 'all': True searches stopped container, which fails
    #               due to missing NetworkSettings
    #  in generic filters you can have
    #       "name": "/machine-"
    generic_filters = {'all': False, 'limit': 2}
    meta_filters = {}
    m = dockerprovider.MachineManager(provider, version='1.15')
    ret = m.search(generic_filters, meta_filters)
    assert ret


def test_destroy():
    """Destroy a machine.

    :param machine_uuid: UUID that uniquely identifies the machine.
    """
    wait_spawning = True
    parameters = {
        'image': {'name': 'busybox'},
        'flavor': {'name': 'latest'},
        'meta': {
            'volumes': '/mnt/foo',
            'command': split('/bin/sleep 20')
        },
        'number_machines': 1,

    }
    m = dockerprovider.MachineManager(provider, version='1.15')
    ret = m.create(parameters, wait_spawning)[0]
    machine_uuid = ret.uuid
    log.warn(ret)
    created_containers.append(machine_uuid)
    m.destroy(machine_uuid=machine_uuid)


def assign_public_ip():
    """Assign public IP address to a machine.

    :param machine: Reference to a machine.
    :param pool: Pool from where the address will be withdrawn.
    """
    raise NotImplementedError

def remove_public_ip():
    """Remove public addresses assigned to a machine.

    :param machine: Reference to a machine.
    """
    raise NotImplementedError


