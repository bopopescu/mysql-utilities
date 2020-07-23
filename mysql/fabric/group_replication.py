#
# Copyright (c) 2013,2014, Oracle and/or its affiliates. All rights reserved.
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

"""This module contains the functionality used to setup relication between
the mains of Groups. The module contains utility methods for doing the
following

- Setting up replication between two groups - setup_group_replication
- Starting the subordinates of a particular group - start_group_subordinates
- Stopping the subordinates of a particular group - stop_group_subordinates
- Stopping a given group that is a subordinate - stop_group_subordinate

Note that each group is aware of the main group it replicates from and also
of the subordinate groups that replicate from it. This information helps in performing
clean up during the event of a "main changing operation" in the group.

As a design alternative these methods could have have been moved into the
mysql.fabric.server.py module or into the mysql.py.replication.py module. But
this would have resulted in tight coupling between the HA and the Server layers
and also would have resulted in a circular dependency, since the replication
module already has a reference to the server module.
"""
import logging

from mysql.fabric.server import Group,  MySQLServer
import mysql.fabric.replication as _replication
import mysql.fabric.errors as _errors

_LOGGER = logging.getLogger(__name__)

GROUP_REPLICATION_GROUP_NOT_FOUND_ERROR = "Group not found %s"
GROUP_REPLICATION_GROUP_MASTER_NOT_FOUND_ERROR = "Group main not found %s"
GROUP_MASTER_NOT_RUNNING = "Group main not running %s"
GROUP_REPLICATION_SERVER_ERROR = \
    "Error accessing server (%s) while configuring group replication. %s."

def start_group_subordinates(main_group_id):
    """Start the subordinate groups for the given main group. The
    method will be used in the events that requires, a group, that
    has registered subordinates to start them. An example would be
    enable shard, enable shard requires that a group start all
    the subordinates that are registered with it.

    :param main_group_id: The main group ID. The ID belongs to the main
                            whose subordinates need to be started.
    """
    # Fetch the main group corresponding to the main group
    # ID.
    main_group = Group.fetch(main_group_id)
    if main_group is None:
        raise _errors.GroupError(GROUP_REPLICATION_GROUP_NOT_FOUND_ERROR % \
                                                (main_group_id, ))

    # Setup replication with mains of the groups registered as main
    # groups. main_group.subordinate_group_ids contains the list of the group
    # IDs that are subordinates to this main. Iterate through this list and start
    # replication with the registered subordinates.
    for subordinate_group_id in main_group.subordinate_group_ids:
        subordinate_group = Group.fetch(subordinate_group_id)
        # Setup replication with the subordinate group.
        try:
            setup_group_replication(main_group_id, subordinate_group.group_id)
        except (_errors.GroupError, _errors.DatabaseError) as error:
            _LOGGER.warning(
                "Error while configuring group replication between "
                "(%s) and (%s): (%s).", main_group_id, subordinate_group.group_id,
                error
            )

def stop_group_subordinates(main_group_id):
    """Stop the group subordinates for the given main group. This will be used
    for use cases that required all the subordinates replicating from this group to
    be stopped. An example use case would be disabling a shard.

    :param main_group_id: The main group ID.
    """
    main_group = Group.fetch(main_group_id)
    if main_group is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_NOT_FOUND_ERROR % \
        (main_group_id, ))

    # Stop the replication on all of the registered subordinates for the group.
    for subordinate_group_id in main_group.subordinate_group_ids:

        subordinate_group = Group.fetch(subordinate_group_id)
        # Fetch the Subordinate Group and the main of the Subordinate Group
        subordinate_group_main = MySQLServer.fetch(subordinate_group.main)
        if subordinate_group_main is None:
            _LOGGER.warning(
                GROUP_REPLICATION_GROUP_MASTER_NOT_FOUND_ERROR % \
                (subordinate_group.main, )
            )
            continue

        if not server_running(subordinate_group_main):
            # The server is already down. we cannot connect to it to stop
            # replication.
            continue

        try:
            subordinate_group_main.connect()
            _replication.stop_subordinate(subordinate_group_main, wait=True)
            # Reset the subordinate to remove the reference of the main so
            # that when the server is used as a subordinate next it does not
            # complaint about having a different main.
            _replication.reset_subordinate(subordinate_group_main, clean=True)
        except _errors.DatabaseError as error:
            # Server is not accessible, unable to connect to the server.
            _LOGGER.warning(
                "Error while unconfiguring group replication between "
                "(%s) and (%s): (%s).", main_group_id, subordinate_group.group_id,
                error
            )
            continue

def stop_group_subordinate(group_main_id,  group_subordinate_id,  clear_ref):
    """Stop the subordinate on the subordinate group. This utility method is the
    completement of the setup_group_replication method and is
    used to stop the replication on the subordinate group. Given a main group ID
    and the subordinate group ID the method stops the subordinate on the subordinate
    group and updates the references on both the main and the
    subordinate group.

    :param group_main_id: The id of the main group.
    :param group_subordinate_id: The id of the subordinate group.
    :param clear_ref: The parameter indicates if the stop_group_subordinate
                                needs to clear the references to the group's
                                subordinates. For example when you do a disable
                                shard the shard group still retains the
                                references to its subordinates, since when enabled
                                it needs to enable the replication.
    """
    main_group = Group.fetch(group_main_id)
    subordinate_group = Group.fetch(group_subordinate_id)

    if main_group is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_NOT_FOUND_ERROR % (group_main_id, ))

    if subordinate_group is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_NOT_FOUND_ERROR % (group_subordinate_id, ))

    subordinate_group_main = MySQLServer.fetch(subordinate_group.main)
    if subordinate_group_main is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_MASTER_NOT_FOUND_ERROR %
          (subordinate_group.main, ))

    if not server_running(subordinate_group_main):
        #The server is already down. We cannot connect to it to stop
        #replication.
        return
    try:
        subordinate_group_main.connect()
    except _errors.DatabaseError:
        #Server is not accessible, unable to connect to the server.
        return

    #Stop replication on the main of the group and clear the references,
    #if clear_ref has been set.
    _replication.stop_subordinate(subordinate_group_main, wait=True)
    _replication.reset_subordinate(subordinate_group_main,  clean=True)
    if clear_ref:
        subordinate_group.remove_main_group_id()
        main_group.remove_subordinate_group_id(group_subordinate_id)

def setup_group_replication(group_main_id,  group_subordinate_id):
    """Sets up replication between the mains of the two groups and
    updates the references to the groups in each other.

    :param group_main_id: The group whose main will act as the main
                                             in the replication setup.
    :param group_subordinate_id: The group whose main will act as the subordinate in the
                                      replication setup.
    """
    group_main = Group.fetch(group_main_id)
    group_subordinate = Group.fetch(group_subordinate_id)

    if group_main is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_NOT_FOUND_ERROR % (group_main_id, ))

    if group_subordinate is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_NOT_FOUND_ERROR % (group_subordinate_id, ))

    if group_main.main is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_MASTER_NOT_FOUND_ERROR % "")

    if group_subordinate.main is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_MASTER_NOT_FOUND_ERROR % "")

    #Main is the main of the Global Group. We replicate from here to
    #the mains of all the shard Groups.
    main = MySQLServer.fetch(group_main.main)
    if main is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_MASTER_NOT_FOUND_ERROR % \
        (group_main.main, ))

    #Get the main of the shard Group.
    subordinate = MySQLServer.fetch(group_subordinate.main)
    if subordinate is None:
        raise _errors.GroupError \
        (GROUP_REPLICATION_GROUP_MASTER_NOT_FOUND_ERROR % \
        (group_subordinate.main, ))

    if not server_running(main):
        #The server is already down. We cannot connect to it to setup
        #replication.
        raise _errors.GroupError \
        (GROUP_MASTER_NOT_RUNNING % (group_main.group_id, ))

    try:
        main.connect()
    except _errors.DatabaseError as error: 
        #Server is not accessible, unable to connect to the server.
        raise _errors.GroupError(
            GROUP_REPLICATION_SERVER_ERROR %  (group_subordinate.main, error)
        )

    if not server_running(subordinate):
        #The server is already down. We cannot connect to it to setup
        #replication.
        raise _errors.GroupError \
            (GROUP_MASTER_NOT_RUNNING % (group_subordinate.group_id, ))

    try:
        subordinate.connect()
    except _errors.DatabaseError as error:
        raise _errors.GroupError(
            GROUP_REPLICATION_SERVER_ERROR %  (group_main.main, error)
        )

    _replication.stop_subordinate(subordinate, wait=True)

    #clear references to old mains in the subordinate
    _replication.reset_subordinate(subordinate,  clean=True)

    _replication.switch_main(subordinate, main, main.user, main.passwd)

    _replication.start_subordinate(subordinate, wait=True)

    try:
        group_main.add_subordinate_group_id(group_subordinate_id)
        group_subordinate.add_main_group_id(group_main_id)
    except _errors.DatabaseError:
        #If there is an error while adding a reference to
        #the subordinate group or a main group, it means that
        #the subordinate group was already added and the error
        #is happening because the group was already registered.
        #Ignore this error.
        pass

def server_running(server):
    """Check if the server is in the running state.

    :param server: The MySQLServer object whose mode needs to be checked.

    :return: True if server is in the running state.
             False otherwise.
    """
    if server.status in [MySQLServer.FAULTY]:
        return False
    return True
