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

""" This module contains functions that are called from the services interface
and change MySQL state. Notice though that after a failure the system does no
undo the changes made through the execution of these functions.
"""
from mysql.fabric import (
    errors as _errors,
    replication as _replication,
    server as _server,
)

CONFIG_NOT_FOUND = "Configuration option not found %s . %s"
GROUP_MASTER_NOT_FOUND = "Group main not found"

def switch_main(subordinate, main):
    """Make subordinate point to main.

    :param subordinate: Subordinate.
    :param main: Main.
    """
    _replication.stop_subordinate(subordinate, wait=True)
    _replication.switch_main(subordinate, main, main.user, main.passwd)
    subordinate.read_only = True
    _replication.start_subordinate(subordinate, wait=True)


def set_read_only(server, read_only):
    """Set server to read only mode.

    :param read_only: Either read-only or not.
    """
    server.read_only = read_only


def reset_subordinate(subordinate):
    """Stop subordinate and reset it.

    :param subordinate: subordinate.
    """
    _replication.stop_subordinate(subordinate, wait=True)
    _replication.reset_subordinate(subordinate, clean=True)


def process_subordinate_backlog(subordinate):
    """Wait until subordinate processes its backlog.

    :param subordinate: subordinate.
    """
    _replication.stop_subordinate(subordinate, wait=True)
    _replication.start_subordinate(subordinate, threads=("SQL_THREAD", ), wait=True)
    subordinate_status = _replication.get_subordinate_status(subordinate)[0]
    _replication.wait_for_subordinate(
        subordinate, subordinate_status.Main_Log_File, subordinate_status.Read_Main_Log_Pos
     )


def synchronize(subordinate, main):
    """Synchronize a subordinate with a main and after that stop the subordinate.

    :param subordinate: Subordinate.
    :param main: Main.
    """
    _replication.sync_subordinate_with_main(subordinate, main, timeout=0)


def stop_subordinate(subordinate):
    """Stop subordinate.

    :param subordinate: Subordinate.
    """
    _replication.stop_subordinate(subordinate, wait=True)

def read_config_value(config, config_group, config_name):
    """Read the value of the configuration option from the config files.

    :param config: The config class that encapsulates the config parsing
                    logic.
    :param config_group: The configuration group to which the configuration
                        belongs
    :param config_name: The name of the configuration that needs to be read,
    """
    config_value = None

    try:
        config_value = config.get(config_group, config_name)
    except AttributeError:
        pass

    if config_value is None:
        raise _errors.ConfigurationError(CONFIG_NOT_FOUND %
                                        (config_group, config_name))

    return config_value

def is_valid_binary(binary):
    """Prints if the binary was found in the given path.

    :param binary: The full path to the binary that needs to be verified.

    :return True: If the binary was found
        False: If the binary was not found.
    """
    import os
    return os.path.isfile(binary) and os.access(binary, os.X_OK)

def fetch_backup_server(source_group):
    """Fetch a spare, subordinate or main from a group in that order of
    availability. Find any spare (no criteria), if there is no spare, find
    a secondary(no criteria) and if there is no secondary the main.

    :param source_group: The group from which the server needs to
                         be fetched.
    """
    #Get a subordinate server whose status is spare.
    backup_server = None
    for server in source_group.servers():
        if server.status == "SPARE":
            backup_server = server

    #If there is no spare check if a running subordinate is available
    if backup_server is None:
        for server in source_group.servers():
            if source_group.main != server.uuid and \
                server.status == "SECONDARY":
                backup_server = server

    #If there is no running subordinate just use the main
    if backup_server is None:
        backup_server = _server.MySQLServer.fetch(source_group.main)

    #If there is no main throw an exception
    if backup_server is None:
        raise _errors.ShardingError(GROUP_MASTER_NOT_FOUND)

    return backup_server
