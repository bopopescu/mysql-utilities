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

"""This module provides the necessary interfaces for performing administrative
tasks on replication.
"""
import re
import logging
import uuid as _uuid

import mysql.fabric.services.utils as _utils

from  mysql.fabric import (
    events as _events,
    group_replication as _group_replication,
    server as _server,
    replication as _replication,
    errors as _errors,
)

from mysql.fabric.command import (
    ProcedureGroup,
)

from mysql.fabric.services.server import (
    _retrieve_server
)

_LOGGER = logging.getLogger(__name__)

# Find out which operation should be executed.
DEFINE_HA_OPERATION = _events.Event()
# Find a subordinate that was not failing to keep with the main's pace.
FIND_CANDIDATE_FAIL = _events.Event("FAIL_OVER")
# Check if the candidate is properly configured to become a main.
CHECK_CANDIDATE_FAIL = _events.Event()
# Wait until all subordinates or a candidate process the relay log.
WAIT_SLAVE_FAIL = _events.Event()
# Find a subordinate that is not failing to keep with the main's pace.
FIND_CANDIDATE_SWITCH = _events.Event()
# Check if the candidate is properly configured to become a main.
CHECK_CANDIDATE_SWITCH = _events.Event()
# Block any write access to the main.
BLOCK_WRITE_SWITCH = _events.Event()
# Wait until all subordinates synchronize with the main.
WAIT_SLAVES_SWITCH = _events.Event()
# Enable the new main by making subordinates point to it.
CHANGE_TO_CANDIDATE = _events.Event()
class PromoteMain(ProcedureGroup):
    """Promote a server into main.

    If users just want to update the state store and skip provisioning steps
    such as configuring replication, the update_only parameter must be set to
    true. Otherwise, the following happens.

    If the main within a group fails, a new main is either automatically
    or manually selected among the subordinates in the group. The process of
    selecting and setting up a new main after detecting that the current
    main failed is known as failover.

    It is also possible to switch to a new main when the current one is still
    alive and kicking. The process is known as switchover and may be used, for
    example, when one wants to take the current main off-line for
    maintenance.

    If a subordinate is not provided, the best candidate to become the new main is
    found. Any candidate must have the binary log enabled, should
    have logged the updates executed through the SQL Thread and both
    candidate and main must belong to the same group. The smaller the lag
    between subordinate and the main the better. So the candidate which satisfies
    the requirements and has the smaller lag is chosen to become the new
    main.

    In the failover operation, after choosing a candidate, one makes the subordinates
    point to the new main and updates the state store setting the new main.

    In the switchover operation, after choosing a candidate, any write access
    to the current main is disabled and the subordinates are synchronized with it.
    Failures during the synchronization that do not involve the candidate subordinate
    are ignored. Then subordinates are stopped and configured to point to the new
    main and the state store is updated setting the new main.
    """
    group_name = "group"
    command_name = "promote"

    def execute(self, group_id, subordinate_id=None, update_only=False,
                synchronous=True):
        """Promote a new main.

        :param uuid: Group's id.
        :param subordinate_id: Candidate's UUID or HOST:PORT.
        :param update_only: Only update the state store and skip provisioning.
        :param synchronous: Whether one should wait until the execution finishes
                            or not.

        In what follows, one will find a figure that depicts the sequence of
        event that happen during a promote operation. The figure is split in
        two pieces and names are abbreviated in order to ease presentation:

        .. seqdiag::

          diagram {
            activation = none;
            === Schedule "find_candidate" ===
            fail_over --> executor [ label = "schedule(find_candidate)" ];
            fail_over <-- executor;
            === Execute "find_candidate" and schedule "check_candidate" ===
            executor -> find_candidate [ label = "execute(find_candidate)" ];
            find_candidate --> executor [ label = "schedule(check_candidate)" ];
            find_candidate <-- executor;
            executor <- find_candidate;
            === Execute "check_candidate" and schedule "wait_subordinate" ===
            executor -> check_candidate [ label = "execute(check_candidate)" ];
            check_candidate --> executor [ label = "schedule(wait_subordinate)" ];
            check_candidate <-- executor;
            executor <- check_candidate;
            === Continue in the next diagram ===
          }

        .. seqdiag::

          diagram {
            activation = none;
            edge_length = 300;
            === Continuation from previous diagram ===
            === Execute "wait_subordinates" and schedule "change_to_candidate" ===
            executor -> wait_subordinate [ label = "execute(wait_subordinate)" ];
            wait_subordinate --> executor [ label = "schedule(change_to_candidate)" ];
            wait_subordinate <-- executor;
            executor <- wait_subordinate;
            === Execute "change_to_candidate" ===
            executor -> change_to_candidate [ label = "execute(change_to_candidate)" ];
            change_to_candidate <- executor;
          }

        In what follows, one will find a figure that depicts the sequence of
        events that happen during the switchover operation. The figure is split
        in two pieces and names are abreviated in order to ease presentation:

        .. seqdiag::

          diagram {
            activation = none;
            === Schedule "find_candidate" ===
            switch_over --> executor [ label = "schedule(find_candidate)" ];
            switch_over <-- executor;
            === Execute "find_candidate" and schedule "check_candidate" ===
            executor -> find_candidate [ label = "execute(find_candidate)" ];
            find_candidate --> executor [ label = "schedule(check_candidate)" ];
            find_candidate <-- executor;
            executor <- find_candidate;
            === Execute "check_candidate" and schedule "block_write" ===
            executor -> check_candidate [ label = "execute(check_candidate)" ];
            check_candidate --> executor [ label = "schedule(block_write)" ];
            check_candidate <-- executor;
            executor <- check_candidate;
            === Execute "block_write" and schedule "wait_subordinates" ===
            executor -> block_write [ label = "execute(block_write)" ];
            block_write --> executor [ label = "schedule(wait_subordinates)" ];
            block_write <-- executor;
            executor <- block_write;
            === Continue in the next diagram ===
          }

        .. seqdiag::

          diagram {
            activation = none;
            edge_length = 350;
            === Continuation from previous diagram ===
            === Execute "wait_subordinates_catch" and schedule "change_to_candidate" ===
            executor -> wait_subordinates [ label = "execute(wait_subordinates)" ];
            wait_subordinates --> executor [ label = "schedule(change_to_candidate)" ];
            wait_subordinates <-- executor;
            executor <- wait_subordinates;
            === Execute "change_to_candidate" ===
            executor -> change_to_candidate [ label = "execute(change_to_candidate)" ];
            executor <- change_to_candidate;
          }
        """
        procedures = _events.trigger(
            DEFINE_HA_OPERATION, self.get_lockable_objects(), group_id,
            subordinate_id, update_only
        )
        return self.wait_for_procedures(procedures, synchronous)

@_events.on_event(DEFINE_HA_OPERATION)
def _define_ha_operation(group_id, subordinate_id, update_only):
    """Define which operation must be called based on the main's status
    and whether the candidate subordinate is provided or not.
    """
    fail_over = True

    group = _server.Group.fetch(group_id)
    if not group:
        raise _errors.GroupError("Group (%s) does not exist." % (group_id, ))

    if update_only and not subordinate_id:
        raise _errors.ServerError(
            "The new main must be specified through --subordinate-uuid if "
            "--update-only is set."
        )

    if group.main:
        main = _server.MySQLServer.fetch(group.main)
        if main.status != _server.MySQLServer.FAULTY:
            if update_only:
                _do_block_write_main(group_id, str(group.main), update_only)
            fail_over = False

    if update_only:
        # Check whether the server is registered or not.
        _retrieve_server(subordinate_id, group_id)
        _change_to_candidate(group_id, subordinate_id, update_only)
        return

    if fail_over:
        if not subordinate_id:
            _events.trigger_within_procedure(FIND_CANDIDATE_FAIL, group_id)
        else:
            _events.trigger_within_procedure(CHECK_CANDIDATE_FAIL, group_id,
                                             subordinate_id
            )
    else:
        if not subordinate_id:
            _events.trigger_within_procedure(FIND_CANDIDATE_SWITCH, group_id)
        else:
            _events.trigger_within_procedure(CHECK_CANDIDATE_SWITCH, group_id,
                                             subordinate_id
            )

# Block any write access to the main.
BLOCK_WRITE_DEMOTE = _events.Event()
# Wait until all subordinates synchronize with the main.
WAIT_SLAVES_DEMOTE = _events.Event()
class DemoteMain(ProcedureGroup):
    """Demote the current main if there is one.

    If users just want to update the state store and skip provisioning steps
    such as configuring replication, the update_only parameter must be set to
    true. Otherwise any write access to the main is blocked, subordinates are
    synchronized with the main, stopped and their replication configuration
    reset. Note that no subordinate is promoted as main.
    """
    group_name = "group"
    command_name = "demote"

    def execute(self, group_id, update_only=False, synchronous=True):
        """Demote the current main if there is one.

        :param uuid: Group's id.
        :param update_only: Only update the state store and skip provisioning.
        :param synchronous: Whether one should wait until the execution finishes
                            or not.

        In what follows, one will find a figure that depicts the sequence of
        event that happen during the demote operation. To ease the presentation
        some names are abbreivated:

        .. seqdiag::

          diagram {
            activation = none;
            === Schedule "block_write" ===
            demote --> executor [ label = "schedule(block_write)" ];
            demote <-- executor;
            === Execute "block_write" and schedule "wait_subordinates" ===
            executor -> block_write [ label = "execute(block_write)" ];
            block_write --> executor [ label = "schedule(wait_subordinates)" ];
            block_write <-- executor;
            executor <- block_write;
            === Execute "wait_subordinates" ===
            executor -> wait_subordinates [ label = "execute(wait_subordinates)" ];
            wait_subordinates --> executor;
            wait_subordinates <-- executor;
            executor <- wait_subordinates;
          }
        """
        procedures = _events.trigger(
            BLOCK_WRITE_DEMOTE, self.get_lockable_objects(), group_id,
            update_only
        )
        return self.wait_for_procedures(procedures, synchronous)

@_events.on_event(FIND_CANDIDATE_SWITCH)
def _find_candidate_switch(group_id):
    """Find the best subordinate to replace the current main.
    """
    subordinate_uuid = _do_find_candidate(group_id, FIND_CANDIDATE_SWITCH)
    _events.trigger_within_procedure(CHECK_CANDIDATE_SWITCH, group_id,
                                     subordinate_uuid)

def _do_find_candidate(group_id, event):
    """Find the best candidate in a group that may be used to replace the
    current main if there is any.

    It chooses the subordinate that has processed more transactions and may become a
    main, e.g. has the binary log enabled. This function does not consider
    purged transactions and delays in the subordinate while picking up a subordinate.

    :param group_id: Group's id from where a candidate will be chosen.
    :return: Return the uuid of the best candidate to become a main in the
             group.
    """
    forbidden_status = (
        _server.MySQLServer.FAULTY,
        _server.MySQLServer.SPARE,
        _server.MySQLServer.CONFIGURING
    )
    group = _server.Group.fetch(group_id)

    main_uuid = None
    if group.main:
        main_uuid = str(group.main)

    chosen_uuid = None
    chosen_gtid_status = None
    for candidate in group.servers():
        if main_uuid != str(candidate.uuid) and \
            candidate.status not in forbidden_status:
            try:
                candidate.connect()
                gtid_status = candidate.get_gtid_status()
                main_issues, why_main_issues = \
                    _replication.check_main_issues(candidate)
                subordinate_issues = False
                why_subordinate_issues = {}
                if event == FIND_CANDIDATE_SWITCH:
                    subordinate_issues, why_subordinate_issues = \
                        _replication.check_subordinate_issues(candidate)
                has_valid_main = (main_uuid is None or \
                    _replication.subordinate_has_main(candidate) == main_uuid)
                can_become_main = False
                if chosen_gtid_status:
                    n_trans = 0
                    try:
                        n_trans = _replication.get_subordinate_num_gtid_behind(
                            candidate, chosen_gtid_status
                            )
                    except _errors.InvalidGtidError:
                        pass
                    if n_trans == 0 and not main_issues and \
                        has_valid_main and not subordinate_issues:
                        chosen_gtid_status = gtid_status
                        chosen_uuid = str(candidate.uuid)
                        can_become_main = True
                elif not main_issues and has_valid_main and \
                    not subordinate_issues:
                    chosen_gtid_status = gtid_status
                    chosen_uuid = str(candidate.uuid)
                    can_become_main = True
                if not can_become_main:
                    _LOGGER.warning(
                        "Candidate (%s) cannot become a main due to the "
                        "following reasons: issues to become a "
                        "main (%s), prerequistes as a subordinate (%s), valid "
                        "main (%s).", candidate.uuid, why_main_issues,
                        why_subordinate_issues, has_valid_main
                        )
            except _errors.DatabaseError as error:
                _LOGGER.warning(
                    "Error accessing candidate (%s): %s.", candidate.uuid,
                    error
                )

    if not chosen_uuid:
        raise _errors.GroupError(
            "There is no valid candidate that can be automatically "
            "chosen in group (%s). Please, choose one manually." %
            (group_id, )
        )
    return chosen_uuid

@_events.on_event(CHECK_CANDIDATE_SWITCH)
def _check_candidate_switch(group_id, subordinate_id):
    """Check if the candidate has all the features to become the new
    main.
    """
    allowed_status = (_server.MySQLServer.SECONDARY, _server.MySQLServer.SPARE)
    group = _server.Group.fetch(group_id)

    if not group.main:
        raise _errors.GroupError(
            "Group (%s) does not contain a valid "
            "main. Please, run a promote or failover." % (group_id, )
        )

    subordinate = _retrieve_server(subordinate_id, group_id)
    subordinate.connect()

    if group.main == subordinate.uuid:
        raise _errors.ServerError(
            "Candidate subordinate (%s) is already main." % (subordinate_id, )
            )

    main_issues, why_main_issues = _replication.check_main_issues(subordinate)
    if main_issues:
        raise _errors.ServerError(
            "Server (%s) is not a valid candidate subordinate "
            "due to the following reason(s): (%s)." %
            (subordinate.uuid, why_main_issues)
            )

    subordinate_issues, why_subordinate_issues = _replication.check_subordinate_issues(subordinate)
    if subordinate_issues:
        raise _errors.ServerError(
            "Server (%s) is not a valid candidate subordinate "
            "due to the following reason: (%s)." %
            (subordinate.uuid, why_subordinate_issues)
            )

    main_uuid = _replication.subordinate_has_main(subordinate)
    if main_uuid is None or group.main != _uuid.UUID(main_uuid):
        raise _errors.GroupError(
            "The group's main (%s) is different from the candidate's "
            "main (%s)." % (group.main, main_uuid)
            )

    if subordinate.status not in allowed_status:
        raise _errors.ServerError("Server (%s) is faulty." % (subordinate_id, ))

    _events.trigger_within_procedure(
        BLOCK_WRITE_SWITCH, group_id, main_uuid, str(subordinate.uuid)
        )

@_events.on_event(BLOCK_WRITE_SWITCH)
def _block_write_switch(group_id, main_uuid, subordinate_uuid):
    """Block and disable write access to the current main.
    """
    _do_block_write_main(group_id, main_uuid)
    _events.trigger_within_procedure(WAIT_SLAVES_SWITCH, group_id,
        main_uuid, subordinate_uuid
        )

def _do_block_write_main(group_id, main_uuid, update_only=False):
    """Block and disable write access to the current main.

    Note that connections are not killed and blocking the main
    may take some time.
    """
    main = _server.MySQLServer.fetch(_uuid.UUID(main_uuid))
    assert(main.status == _server.MySQLServer.PRIMARY)
    main.mode = _server.MySQLServer.READ_ONLY
    main.status = _server.MySQLServer.SECONDARY

    if not update_only:
        main.connect()
        _utils.set_read_only(main, True)

    # Temporarily unset the main in this group.
    group = _server.Group.fetch(group_id)
    _set_group_main_replication(group, None)

    # At the end, we notify that a server was demoted.
    # Any function that implements this event should not
    # run any action that updates Fabric. The event was
    # designed to trigger external actions such as:
    #
    # . Updating an external entity.
    #
    # . Fencing off a server.
    _events.trigger("SERVER_DEMOTED", set([group_id]),
        group_id, str(main.uuid)
    )

@_events.on_event(WAIT_SLAVES_SWITCH)
def _wait_subordinates_switch(group_id, main_uuid, subordinate_uuid):
    """Synchronize candidate with main and also all the other subordinates.

    Note that this can be optimized as one may determine the set of
    subordinates that must be synchronized with the main.
    """
    main = _server.MySQLServer.fetch(_uuid.UUID(main_uuid))
    main.connect()
    subordinate = _server.MySQLServer.fetch(_uuid.UUID(subordinate_uuid))
    subordinate.connect()

    _utils.synchronize(subordinate, main)
    _do_wait_subordinates_catch(group_id, main, [subordinate_uuid])

    _events.trigger_within_procedure(CHANGE_TO_CANDIDATE, group_id, subordinate_uuid)

def _do_wait_subordinates_catch(group_id, main, skip_servers=None):
    """Synchronize subordinates with main.
    """
    skip_servers = skip_servers or []
    skip_servers.append(str(main.uuid))

    group = _server.Group.fetch(group_id)
    for server in group.servers():
        if str(server.uuid) not in skip_servers:
            try:
                server.connect()
                used_main_uuid = _replication.subordinate_has_main(server)
                if  str(main.uuid) == used_main_uuid:
                    _utils.synchronize(server, main)
                else:
                    _LOGGER.debug("Subordinate (%s) has a different main "
                        "from group (%s).", server.uuid, group_id)
            except _errors.DatabaseError as error:
                _LOGGER.debug(
                    "Error synchronizing subordinate (%s): %s.", server.uuid,
                    error
                )

@_events.on_event(CHANGE_TO_CANDIDATE)
def _change_to_candidate(group_id, main_uuid, update_only=False):
    """Switch to candidate subordinate.
    """
    forbidden_status = (_server.MySQLServer.FAULTY, )
    main = _server.MySQLServer.fetch(_uuid.UUID(main_uuid))
    main.mode = _server.MySQLServer.READ_WRITE
    main.status = _server.MySQLServer.PRIMARY

    if not update_only:
        # Prepare the server to be the main
        main.connect()
        _utils.reset_subordinate(main)
        _utils.set_read_only(main, False)

    group = _server.Group.fetch(group_id)
    _set_group_main_replication(group, main.uuid, update_only)

    if not update_only:
        # Make subordinates point to the main.
        for server in group.servers():
            if server.uuid != _uuid.UUID(main_uuid) and \
                server.status not in forbidden_status:
                try:
                    server.connect()
                    _utils.switch_main(server, main)
                except _errors.DatabaseError as error:
                    _LOGGER.debug(
                        "Error configuring subordinate (%s): %s.", server.uuid, error
                    )

    # At the end, we notify that a server was promoted.
    # Any function that implements this event should not
    # run any action that updates Fabric. The event was
    # designed to trigger external actions such as:
    #
    # . Updating an external entity.
    _events.trigger("SERVER_PROMOTED", set([group_id]),
        group_id, main_uuid
    )

@_events.on_event(FIND_CANDIDATE_FAIL)
def _find_candidate_fail(group_id):
    """Find the best candidate to replace the failed main.
    """
    subordinate_uuid = _do_find_candidate(group_id, FIND_CANDIDATE_FAIL)
    _events.trigger_within_procedure(CHECK_CANDIDATE_FAIL, group_id,
                                     subordinate_uuid)

@_events.on_event(CHECK_CANDIDATE_FAIL)
def _check_candidate_fail(group_id, subordinate_id):
    """Check if the candidate has all the prerequisites to become the new
    main.
    """
    allowed_status = (_server.MySQLServer.SECONDARY, _server.MySQLServer.SPARE)
    group = _server.Group.fetch(group_id)

    subordinate = _retrieve_server(subordinate_id, group_id)
    subordinate.connect()

    if group.main == subordinate.uuid:
        raise _errors.ServerError(
            "Candidate subordinate (%s) is already main." % (subordinate_id, )
            )

    main_issues, why_main_issues = _replication.check_main_issues(subordinate)
    if main_issues:
        raise _errors.ServerError(
            "Server (%s) is not a valid candidate subordinate "
            "due to the following reason(s): (%s)." %
            (subordinate.uuid, why_main_issues)
            )

    if subordinate.status not in allowed_status:
        raise _errors.ServerError("Server (%s) is faulty." % (subordinate_id, ))

    _events.trigger_within_procedure(WAIT_SLAVE_FAIL, group_id, str(subordinate.uuid))

@_events.on_event(WAIT_SLAVE_FAIL)
def _wait_subordinate_fail(group_id, subordinate_uuid):
    """Wait until a subordinate processes its backlog.
    """
    subordinate = _server.MySQLServer.fetch(_uuid.UUID(subordinate_uuid))
    subordinate.connect()

    try:
        _utils.process_subordinate_backlog(subordinate)
    except _errors.DatabaseError as error:
        _LOGGER.warning(
            "Error trying to process transactions in the relay log "
            "for candidate (%s): %s.", subordinate, error
        )

    _events.trigger_within_procedure(CHANGE_TO_CANDIDATE, group_id, subordinate_uuid)

@_events.on_event(BLOCK_WRITE_DEMOTE)
def _block_write_demote(group_id, update_only):
    """Block and disable write access to the current main.
    """
    group = _server.Group.fetch(group_id)
    if not group:
        raise _errors.GroupError("Group (%s) does not exist." % (group_id, ))

    if not group.main:
        raise _errors.GroupError("Group (%s) does not have a main." %
                                 (group_id, ))

    main = _server.MySQLServer.fetch(group.main)
    assert(main.status in \
        (_server.MySQLServer.PRIMARY, _server.MySQLServer.FAULTY)
    )

    if main.status == _server.MySQLServer.PRIMARY:
        main.connect()
        main.mode = _server.MySQLServer.READ_ONLY
        main.status = _server.MySQLServer.SECONDARY
        _utils.set_read_only(main, True)

        if not update_only:
            _events.trigger_within_procedure(
                WAIT_SLAVES_DEMOTE, group_id, str(main.uuid)
            )

    _set_group_main_replication(group, None, update_only)

@_events.on_event(WAIT_SLAVES_DEMOTE)
def _wait_subordinates_demote(group_id, main_uuid):
    """Synchronize subordinates with main.
    """
    main = _server.MySQLServer.fetch(_uuid.UUID(main_uuid))
    main.connect()

    # Synchronize subordinates.
    _do_wait_subordinates_catch(group_id, main)

    # Stop replication threads at all subordinates.
    group = _server.Group.fetch(group_id)
    for server in group.servers():
        try:
            server.connect()
            _utils.stop_subordinate(server)
        except _errors.DatabaseError as error:
            _LOGGER.debug(
                "Error waiting for subordinate (%s) to stop: %s.", server.uuid,
                error
            )

def _set_group_main_replication(group, server_id, update_only=False):
    """Set the main for the given group and also reset the
    replication with the other group mains. Any change of main
    for a group will be initiated through this method. The method also
    takes care of resetting the main and the subordinates that are registered
    with this group to connect with the new main.

    The idea is that operations like switchover, failover, promote all are
    finally main changing operations. Hence the right place to handle
    these operations is at the place where the main is being changed.

    The following operations need to be done

    - Stop the subordinate on the old main
    - Stop the subordinates replicating from the old main
    - Start the subordinate on the new main
    - Start the subordinates with the new main

    :param group: The group whose main needs to be changed.
    :param server_id: The server id of the server that is becoming the main.
    :param update_only: Only update the state store and skip provisioning.
    """
    # Set the new main if update-only is true.
    if update_only:
        group.main = server_id
        return

    try:
        # Otherwise, stop the subordinate running on the current main
        if group.main_group_id is not None and group.main is not None:
            _group_replication.stop_group_subordinate(
                group.main_group_id, group.group_id, False
            )
        # Stop the Groups replicating from the current group.
        _group_replication.stop_group_subordinates(group.group_id)
    except (_errors.GroupError, _errors.DatabaseError) as error:
        _LOGGER.error(
            "Error accessing groups related to (%s): %s.", group.group_id,
            error
        )

    # Set the new main
    group.main = server_id

    try:
        # If the main is not None setup the main and the subordinates.
        if group.main is not None:
            # Start the subordinate groups for this group.
            _group_replication.start_group_subordinates(group.group_id)
            if group.main_group_id is not None:
                # Start the subordinate on this group
                _group_replication.setup_group_replication(
                    group.main_group_id, group.group_id
                )
    except (_errors.GroupError, _errors.DatabaseError) as error:
        _LOGGER.error(
            "Error accessing groups related to (%s): %s.", group.group_id,
            error
        )
