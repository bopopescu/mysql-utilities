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
"""Responsible for checking servers' health in a group.
"""
import logging

from  mysql.fabric import (
    server as _server,
    replication as _replication,
    errors as _errors,
)

from mysql.fabric.command import (
    Command,
    CommandResult,
    ResultSet,
)

from mysql.fabric.services.server import (
   DEFAULT_UNREACHABLE_TIMEOUT
)

_LOGGER = logging.getLogger(__name__)

class CheckHealth(Command):
    """Check if any server within a group has failed and report health
    information.
    """
    group_name = "group"
    command_name = "health"

    def execute(self, group_id, timeout=None):
        """Check if any server within a group has failed.

        :param group_id: Group's id.
        :param group_id: Timeout value after which a server is considered
                         unreachable. If None is provided, it assumes the
                         default value in the configuration file.
        """

        group = _server.Group.fetch(group_id)
        if not group:
            raise _errors.GroupError("Group (%s) does not exist." % (group_id, ))

        info = ResultSet(
            names=[
                'uuid', 'is_alive', 'status',
                'is_not_running', 'is_not_configured', 'io_not_running',
                'sql_not_running', 'io_error', 'sql_error'
            ],
            types=[str, bool, str] + [bool] * 4 + [str, str]
        )
        issues = ResultSet(names=['issue'], types=[str])

        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            pass

        for server in group.servers():
            alive = False
            is_main = (group.main == server.uuid)
            status = server.status

            why_subordinate_issues = {}
            # These are used when server is not contactable.
            why_subordinate_issues = {
                'is_not_running': False,
                'is_not_configured': False,
                'io_not_running': False,
                'sql_not_running': False,
                'io_error': False,
                'sql_error': False,
            }

            try:
                alive = server.is_alive(timeout or DEFAULT_UNREACHABLE_TIMEOUT)
                if alive and not is_main:
                    server.connect()
                    subordinate_issues, why_subordinate_issues = \
                        _replication.check_subordinate_issues(server)
                    str_main_uuid = _replication.subordinate_has_main(server)
                    if (group.main is None or str(group.main) != \
                        str_main_uuid) and not subordinate_issues:
                        issues.append_row([
                            "Group has main (%s) but server is connected " \
                            "to main (%s)." % \
                            (group.main, str_main_uuid)
                        ])
            except _errors.DatabaseError:
                alive = False

            info.append_row([
                server.uuid,
                alive,
                status,
                why_subordinate_issues['is_not_running'],
                why_subordinate_issues['is_not_configured'],
                why_subordinate_issues['io_not_running'],
                why_subordinate_issues['sql_not_running'],
                why_subordinate_issues['io_error'],
                why_subordinate_issues['sql_error'],
            ])

        return CommandResult(None, results=[info, issues])
