#
# Copyright (c) 2014 Oracle and/or its affiliates. All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA
#
"""This module provides the necessary means to interact with an OpenStack
provider and has the MachineManager class as the main entry point.
"""
import logging
import time
import uuid as _uuid
import json

import docker as dockerclient

DOCKER = "DOCKER"

from mysql.fabric import (
    errors as _errors,
)

from mysql.fabric.providers import (
    AbstractMachineManager,
    AbstractSnapshotManager,
    catch_exception
)

from mysql.fabric.machine import (
    Machine,
)

from mysql.fabric.utils import (
    kv_to_dict
)

_LOGGER = logging.getLogger(__name__)

SEARCH_PROPERTIES = {
    'mindisk' : ('minDisk', int),
    'minram' : ('minRam', int),
    'ram' : ('ram', int),
    'vcpus' : ('vcpus', int),
    'swap' : ('swap', int),
    'disk' : ('disk', int),
    'rxtx_factor' : ('rxtx_factor', float),
}

def preprocess_meta(meta):
    """Preprocess parameters that will be used to search for resources in
    the cloud.

    This is necessary because the parameters are strings and some of them
    need to be converted to integers or floats. Besides the parameter names
    are case sensitive and must be changed.
    """
    proc_meta = {}
    _LOGGER.warning("Preprocessing meta %s", (meta,))
    for key, value in meta.iteritems():
        key = key.lower()
        if key in SEARCH_PROPERTIES:
            key, convert = SEARCH_PROPERTIES[key]
            try:
                value = convert(value)
            except ValueError as error:
                raise _errors.MachineError(error)
        proc_meta[key] = value
    return proc_meta

def find_resource(meta, finder):
    """Find a resource based on some meta information.

        :param meta - {kwargs}
        :type dict
        :param finder
        :type method
    """
    proc_meta = preprocess_meta(meta)
    resources = finder(**proc_meta)
    if not resources:
        raise _errors.ConfigurationError(
            "There is no resource with the requested properties: %s" %
            (proc_meta, )
        )
    elif len(resources) > 1:
        _LOGGER.warning(
            "There are more than one resource with the requested properties: "
            "(%s). Using (%s).", proc_meta, resources[0]
        )
    _LOGGER.info("Using resource (%s).", resources[0])
    return resources[0]

def keep_waiting(obj, get_info, status):
    """Keep pooling until the status changes.
    Note that this function does not fetch detailed information when there
    is an error and this needs to be improved.
    """
    while ('State' not in obj
           or obj['State']['StartedAt'] == '0001-01-01T00:00:00Z'
           ):
        time.sleep(5)
        obj = get_info(obj['Id'])
    if not obj['State']['Running']:
        raise _errors.MachineError(
            "Unexpected status (%s) when valid statuses were (%s). "
            "Error creating resource (%s)." % (obj['State'], status, str(obj['Id']))
        )

def configure_provider():
    """Configure the OpenStack Provider.
    """
    return (DOCKER, MachineManager, SnapshotManager, 3)

class MachineManager(AbstractMachineManager):
    """Manage an Openstack Machine.

    Note that SSL is not supported yet and this needs to be improved.
    """
    @catch_exception
    def __init__(self, provider, version="1.1"):
        """Constructor for MachineManager object.
        """
        print(self)
        super(MachineManager, self).__init__(provider, version)
        self.__dc = _connect_docker(self.provider, version)
        #self.__ns = _connect_neutron(self.provider, '2.0')

    #@catch_exception
    def create(self, parameters, wait_spawning):
        """Create a Docker Container.

            mysqlfabric server create docker-1 --image name=mysql-enterprise --flavor name=v0
        """
        # Make a copy of the parameters as it will be changed.
        parameters = parameters.copy()

        # Retrieve image's reference by name.
        parameters['image'] = \
            find_resource(parameters['image'], self.__dc.images)
        parameters['image'] = parameters['image']['RepoTags'][0]

        # Create machines.
        machines = []
        number_machines = parameters['number_machines']
        del parameters['number_machines']

        VALID_PARAMETERS = ['image', 'command', 'hostname', 'user', 'detach',
                            'stdin_open', 'tty', 'mem_limit', 'ports', 'environment',
                            'dns', 'volumes', 'volumes_from', 'network_disabled',
                            'name', 'entrypoint', 'cpu_shares', 'working_dir', 'domainname',
                            'memswap_limit', 'cpuset', 'host_config', 'mac_address']
        # Apply docker parameters passed via 'meta' to  parameters.
        parameters.update(parameters.get('meta', {}))
        for k, v in parameters.items():
            if k not in VALID_PARAMETERS:
                 _LOGGER.warning("Skipping parameter %s", {k:v})
                 del parameters[k]

        for n_machine in range(number_machines):
            machine_uuid = str(_uuid.uuid4())
            machine_name = "-".join(["machine", machine_uuid])
            _LOGGER.debug("Creating machine %s %s.", n_machine, machine_name)
            container = self.__dc.create_container(name=machine_name,
                                                  # command="mysqld",
                                                   environment={
                                                       'MYSQL_ROOT_PASSWORD': 'root'
                                                   }
                                                   , **parameters)
            self.__dc.start(container['Id'])
            machines.append(container)

        # Wait until the machine is alive and kicking.
        if wait_spawning:
            for container in machines:
                keep_waiting(
                    container, self.__dc.inspect_container, (False,)
                )

        ret = []
        for container in machines:
            ret.append(self._format_machine(container))

        return ret

    @catch_exception
    def search(self, generic_filters, meta_filters):
        """Return running containers based on the provided filters.

            :param generic_filters: search filter passed to docker-py
            :param meta_filters: filters applied to container.items()

            meta_filters can use any of
            {u'Status': u'Exited (0) 2 days ago',
            u'Created': 1429221117,
            u'Image': u'composetest_test:latest',
            u'Ports': [],
            u'Command': u'/bin/true',
            u'Names': [u'/commandscomposefile_explicit_run_1'],
            u'Id': u'3a2ab911cd96d3908fa797ad7b3c20c5d076e9227d77035c617e71cb9ef8ee06'},
        """
        _LOGGER.warn(
            "Searching for machines using generic filters (%s) and "
            "meta filters (%s).", generic_filters, meta_filters
        )
       
        match = []
        for container in self.__dc.containers(**generic_filters):
            checked = []
            checked_keys = set()
            # why not set(meta_filters)?
            keys = set(meta_filters)
            for key, values in container.items():
                if key in meta_filters.values():
                    checked.append(meta_filters[key] in values)
                    checked_keys.add(key)
            if keys == checked_keys and all(checked):
                match.append(container)

        _LOGGER.debug("Found machines (%s).", match)

        ret = []
        for container in match:
            ret.append(self._format_machine(container))
        return ret

    #@catch_exception
    def destroy(self, machine_uuid):
        """Destroy an OpenStack Machine.
        """
        container_name = self._get_machine(machine_uuid)
        #self.remove_public_ip(machine)
        self.__dc.remove_container(container_name['Id'], v=True, force=True)


    @catch_exception
    def assign_public_ip(self, machine, pool):
        """Assign a public IP address to an OpenStack Machine.
        """
        floating_ip = _create_floating_ip(self.__dc, self.__ns, pool)
        machine.add_floating_ip(floating_ip)
        _LOGGER.info(
            "Associated elastic ip (%s) to machine (%s).", floating_ip.ip,
            str(machine.id)
        )
        return floating_ip.ip

    @catch_exception
    def remove_public_ip(self, machine):
        """Remove all public IP addresses from an OpenStack Machine.
        """
        raise NotImplementedError

    def _get_machine(self, machine_uuid):
        """Return infos about a given container.
        """
        ret = self.__dc.containers(filters={"name": "/machine-" + machine_uuid})
        if len(ret) == 1:
            return ret[0]
        elif len(ret) == 0:
            raise  ValueError("No machine with the given uuid")
        else:
            raise ValueError("More than one matches for uuid")

    def _format_machine(self, container):
        """Format machine data.

        :param machine: Reference to a machine.
        """

        machine = self.__dc.inspect_container(container['Id'])
        addresses = json.dumps(machine['NetworkSettings']['IPAddress'])

        av_host = "-"
        try:
            av_host = getattr(machine, "OS-EXT-SRV-ATTR:hypervisor_hostname")
        except AttributeError:
            pass

        av_zone = "-"
        try:
            av_zone = getattr(machine, "OS-EXT-AZ:availability_zone")
        except AttributeError:
            pass

        new = Machine(uuid=machine['Name'].replace("/machine-", ""),
            provider_id=self.provider.provider_id,
            av_zone=":".join([av_zone, av_host]),
            addresses=addresses
        )
        return new

class SnapshotManager(AbstractSnapshotManager):
    """Manage an Openstack Snapshot (i.e. Image).

    Note that SSL is not supported yet and this needs to be improved.
    """
    @catch_exception
    def __init__(self, provider, version="1.1"):
        """Constructor for MachineManager object.
        """
        super(SnapshotManager, self).__init__(provider, version)
        self.__cs = _connect_docker(self.provider, version)
        self.__ns = _connect_neutron(self.provider, '2.0')

    @catch_exception
    def create(self, machine_uuid, wait_spawning):
        """Create a snapshot from an OpenStack Machine
        """
        machine = self.__cs.servers.get(machine_uuid)
        snapshot_name = "-".join(["snapshot", machine_uuid, str(time.time())])
        snapshot_id = machine.create_image(snapshot_name)
        if wait_spawning:
            image = self.__cs.images.get(snapshot_id)
            keep_waiting(image, self.__cs.images.get, ('QUEUED', 'SAVING'))
        return snapshot_name

    @catch_exception
    def destroy(self, machine_uuid):
        """Destroy a snapshot associated to an OpenStack Machine.
        """
        images = self.__cs.images.list()
        snapshot_name = "-".join(["snapshot", machine_uuid])
        for image in images:
            if snapshot_name in image.name:
                image.delete()



def _create_floating_ip(cs, ns, pool):
    try:
        return cs.floating_ips.create(pool=pool)
        return floating_ip
    except novaclient.exceptions.NotFound:
        # Most likely this means that neutron or a proprietary API is being
        # used. So before aborting the operation, we will try to use the
        # neutron client to create a floating ip.
        pass

    raise _errors.MachineError(
        "Error accessing public network (%s)." % (pool, )
    )


def _connect_neutron(provider, version):
    """Connect to a provider.
    """
    raise  NotImplementedError
    credentials = {
        'username' : provider.username,
        'password' : provider.password,
        'auth_url' : provider.url,
        'tenant_name' : provider.tenant
    }

    fixed_credentials = {}
    _fix_credentials(provider, fixed_credentials)
    credentials['region_name'] = fixed_credentials.get('region_name', None)
    return neutronclient.neutron.client.Client(version, **credentials)

def _connect_docker(provider, version):
    """Connect to a provider.
    """
    return dockerclient.Client(provider.url)

