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

"""This module contains abstractions of MySQL replication features.
"""
import time
import uuid as _uuid
import mysql.fabric.errors as _errors
import mysql.fabric.server as _server

from mysql.fabric.connection import (
    split_host_port,
    MYSQL_DEFAULT_PORT
)

_RPL_USER_QUERY = (
    "SELECT user, host, password != '' as has_password "
    "FROM mysql.user "
    "WHERE repl_subordinate_priv = 'Y'"
)

_MASTER_POS_WAIT = "SELECT MASTER_POS_WAIT(%s, %s, %s)"

_GTID_WAIT = "SELECT WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS(%s, %s)"

IO_THREAD = "IO_THREAD"

SQL_THREAD = "SQL_THREAD"

@_server.server_logging
def get_main_status(server):
    """Return the main status. In order to ease the navigation through
    the result set, a named tuple is always returned. Look up the `SHOW
    MASTER STATUS` command in the MySQL Manual for further details.

    :param server: MySQL Server.
    """
    return server.exec_stmt("SHOW MASTER STATUS", {"columns" : True})

@_server.server_logging
def reset_main(server):
    """Reset the main. Look up the `RESET MASTER` command in the
    MySQL Manual for further details.

    :param server: MySQL Server.
    """
    server.exec_stmt("RESET MASTER")

@_server.server_logging
def has_appropriate_privileges(server):
    """Check whether the current user has the `REPLICATION SLAVE PRIVILEGE`.

    :param server: MySQL Server.
    """
    return server.has_privileges(["REPLICATION SLAVE"])

@_server.server_logging
def check_main_issues(server):
    """Check if there is any issue to make the server a main.

    This method checks if there is any issue to make the server a main.
    and returns a dictionary that contains information on any issue found
    , if there is any. Basically, it checks if the main is alive and
    kicking, if the binary log is enabled, if the GTID is enabled, if the
    server is able to log the updates through the SQL Thread and finally
    if there is a user that has the `REPLICATION SLAVE PRIVILEGE`.

    The dictionary returned may have the following keys::

      status['is_not_running'] = False
      status['is_binlog_not_enabled'] = False
      status['is_gtid_not_enabled'] = False
      status['is_subordinate_updates_not_enabled'] = False
      status['no_rpl_user'] = False

    :param server: MySQL Server.
    :return: Whether there is an issue or not and a dictionary with issues,
             if there is any.

    .. note::

       It does not consider if there are filters or some binary logs have been
       purged and by consequence the associated GTIDs. These are also important
       characteristics before considering a server eligible for becoming a
       main.
    """
    status = {
        'is_not_running' : False,
        'is_binlog_not_enabled' : False,
        'is_gtid_not_enabled' : False,
        'is_subordinate_updates_not_enabled' : False,
        'no_rpl_user' : False
    }

    if not server.is_connected():
        status["is_not_running"] = True
        return True, status

    # Check for binlog.
    if not server.binlog_enabled:
        status["is_binlog_not_enabled"] = True

    # Check for gtid.
    if not server.gtid_enabled:
        status["is_gtid_not_enabled"] = True

    # Check for subordinate updates.
    if not server.get_variable("LOG_SLAVE_UPDATES"):
        status["is_subordinate_updates_not_enabled"] = True

    # See if the current user has the appropriate replication privilege(s)
    if not has_appropriate_privileges(server):
        status["no_rpl_user"] = True

    error = not all([v is False for v in status.itervalues()])
    return error, status

@_server.server_logging
def get_subordinate_status(server, options=None):
    """Return the subordinate status. In order to ease the navigation through
    the result set, a named tuple is always returned. Look up the `SHOW
    SLAVE STATUS` command in the MySQL Manual for further details.

    :param server: MySQL Server.
    :param options: Define how the result is formatted and retrieved.
                    See :meth:`~mysql.fabric.server.MySQLServer.exec_stmt`.
    """
    return server.exec_stmt("SHOW SLAVE STATUS", {"columns" : True})

@_server.server_logging
def is_subordinate_thread_running(server, threads=None):
    """Check to see if subordinate's threads are running smoothly.

    :param server: MySQL Server.
    """
    return _check_condition(server, threads, True)

@_server.server_logging
def subordinate_has_main(server):
    """Return the main's uuid to which the subordinate is connected to.

    :param server: MySQL Server.
    :return: Main's uuid or None.
    :rtype: String.
    """
    ret = get_subordinate_status(server)
    if ret:
        try:
            str_uuid = ret[0].Main_UUID
            _uuid.UUID(str_uuid)
            return str_uuid
        except ValueError:
            pass
    return None

@_server.server_logging
def get_num_gtid(gtids, server_uuid=None):
    """Return the number of transactions represented in GTIDs.

    By default this function considers any server in GTIDs. So if one wants
    to count transactions from a specific server, the parameter server_uuid
    must be defined.

    :param gtids: Set of transactions.
    :param server_uuid: Which server one should consider where None means
                        all.
    """
    sid = None
    difference = 0
    for gtid in gtids.split(","):
        # Exctract the server_uuid and the trx_ids.
        trx_ids = None
        if gtid.find(":") != -1:
            sid, trx_ids = gtid.split(":")
        else:
            if not sid:
                raise _errors.ProgrammingError(
                    "Malformed GTID (%s)." % (gtid, )
                )
            trx_ids = gtid

        # Ignore differences if server_uuid is set and does
        # not match.
        if server_uuid and str(server_uuid).upper() != sid.upper():
            continue

        # Check the difference.
        difference += 1
        if trx_ids.find("-") != -1:
            lgno, rgno = trx_ids.split("-")
            difference += int(rgno) - int(lgno)
    return difference

def get_subordinate_num_gtid_behind(server, main_gtids, main_uuid=None):
    """Get the number of transactions behind the main.

    :param server: MySQL Server.
    :param main_gtids: GTID information retrieved from the main.
        See :meth:`~mysql.fabric.server.MySQLServer.get_gtid_status`.
    :param main_uuid: Main which is used as the basis for comparison.
    :return: Number of transactions behind main.
    """
    gtids = None
    main_gtids = main_gtids[0].GTID_EXECUTED
    subordinate_gtids = server.get_gtid_status()[0].GTID_EXECUTED

    # The subtract function does not accept empty strings.
    if main_gtids == "" and subordinate_gtids != "":
        raise _errors.InvalidGtidError(
            "It is not possible to check the lag when the "
            "main's GTID is empty."
            )
    elif main_gtids == "" and subordinate_gtids == "":
        return 0
    elif subordinate_gtids == "":
        gtids = main_gtids
    else:
        assert (main_gtids != "" and subordinate_gtids != "")
        gtids = server.exec_stmt("SELECT GTID_SUBTRACT(%s,%s)",
                                 {"params": (main_gtids, subordinate_gtids)})[0][0]
        if gtids == "":
            return 0
    return get_num_gtid(gtids, main_uuid)

@_server.server_logging
def start_subordinate(server, threads=None, wait=False, timeout=None):
    """Start the subordinate. Look up the `START SLAVE` command in the MySQL
    Manual for further details.

    :param server: MySQL Server.
    :param threads: Determine which threads shall be started.
    :param wait: Determine whether one shall wait until the thread(s)
                 start(s) or not.
    :type wait: Bool
    :param timeout: Time in seconds after which one gives up waiting for
                    thread(s) to start.

    The parameter `threads` determine which threads shall be started. If
    None is passed as parameter, both the `SQL_THREAD` and the `IO_THREAD`
    are started.
    """
    threads = threads or ()
    server.exec_stmt("START SLAVE " + ", ".join(threads))
    if wait:
        wait_for_subordinate_thread(server, timeout=timeout, wait_for_running=True,
                              threads=threads)

@_server.server_logging
def stop_subordinate(server, threads=None, wait=False, timeout=None):
    """Stop the subordinate. Look up the `STOP SLAVE` command in the MySQL
    Manual for further details.

    :param server: MySQL Server.
    :param threads: Determine which threads shall be stopped.
    :param wait: Determine whether one shall wait until the thread(s)
                 stop(s) or not.
    :type wait: Bool
    :param timeout: Time in seconds after which one gives up waiting for
                    thread(s) to stop.

    The parameter `threads` determine which threads shall be stopped. If
    None is passed as parameter, both the `SQL_THREAD` and the `IO_THREAD`
    are stopped.
    """
    threads = threads or ()
    server.exec_stmt("STOP SLAVE " + ", ".join(threads))
    if wait:
        wait_for_subordinate_thread(server, timeout=timeout, wait_for_running=False,
                              threads=threads)

@_server.server_logging
def reset_subordinate(server, clean=False):
    """Reset the subordinate. Look up the `RESET SLAVE` command in the MySQL
    Manual for further details.

    :param server: MySQL Server.
    :param clean: Do not save main information such as host, user, etc.
    """
    param = "ALL" if clean else ""
    server.exec_stmt("RESET SLAVE %s" % (param, ))

@_server.server_logging
def wait_for_subordinate_thread(server, timeout=None, wait_for_running=True,
                          threads=None):
    """Wait until subordinate's threads stop or start.

    If timeout is None, one waits indefinitely until the condition is
    achieved. If the timeout period expires prior to achieving the
    condition the exception TimeoutError is raised.

    :param server: MySQL Server.
    :param timeout: Number of seconds one waits until the condition is
                    achieved. If it is None, one waits indefinitely.
    :param wait_for_running: If one should check whether threads are
                             running or stopped.
    :type check_if_running: Bool
    :param threads: Which threads should be checked.
    :type threads: `SQL_THREAD` or `IO_THREAD`.
    """
    while (timeout is None or timeout > 0) and \
           not _check_condition(server, threads, wait_for_running):
        time.sleep(1)
        timeout = timeout - 1 if timeout is not None else None
    if not _check_condition(server, threads, wait_for_running):
        raise _errors.TimeoutError(
            "Error waiting for subordinate's thread(s) to either start or stop."
            )

@_server.server_logging
def wait_for_subordinate(server, binlog_file, binlog_pos, timeout=0):
    """Wait for the subordinate to read the main's binlog up to a specified
    position.

    This methods call the MySQL function `SELECT MASTER_POS_WAIT`. If
    the timeout period expires prior to achieving the condition the
    :class:`~mysql.fabric.errors.TimeoutError` exception is raised. If any
    thread is stopped, the :class:`~mysql.fabric.errors.DatabaseError`
    exception is raised.

    :param server: MySQL Server.
    :param binlog_file: Main's binlog file.
    :param binlog_pos: Main's binlog file position.
    :param timeout: Maximum number of seconds to wait for the condition to
                    be achieved.
    """
    # Wait for subordinate to read the main log file
    res = server.exec_stmt(_MASTER_POS_WAIT,
        {"params": (binlog_file, binlog_pos, timeout)}
    )

    if res is None or res[0] is None or res[0][0] is None:
        raise _errors.DatabaseError(
            "Error waiting for subordinate to catch up. Binary log (%s, %s)." %
            (binlog_file, binlog_pos)
        )
    elif res[0][0] == -1:
        raise _errors.TimeoutError(
            "Error waiting for subordinate to catch up. Binary log (%s, %s)." %
            (binlog_file, binlog_pos)
        )

    assert(res[0][0] > -1)

@_server.server_logging
def wait_for_subordinate_status_thread(server, thread, status, timeout=None):
    """Wait until a subordinate's thread exhibits a status.

    The status is a sub-string of the current status: Subordinate_IO_state or
    Subordinate_SQL_Running_State.

    If timeout is None, one waits indefinitely until the condition is
    achieved. If the timeout period expires prior to achieving the
    condition the exception TimeoutError is raised.

    :param server: MySQL Server.
    :param thread: Which thread should be checked.
    :type thread: `SQL_THREAD` or `IO_THREAD`.
    :status: Which status should be checked.
    :type status: string.
    :param timeout: Number of seconds one waits until the condition is
                    achieved. If it is None, one waits indefinitely.
    """
    while (timeout is None or timeout > 0) and \
           not _check_status_condition(server, thread, status):
        time.sleep(1)
        timeout = timeout - 1 if timeout is not None else None
    if not _check_status_condition(server, thread, status):
        raise _errors.TimeoutError(
            "Error waiting for subordinate's thread (%s) to exhibit status (%s)." %
            (thread, status)
        )

@_server.server_logging
def sync_subordinate_with_main(subordinate, main, timeout=0):
    """Synchronizes a subordinate with a main.

    See :func:`wait_for_subordinate_gtid`.

    This function can block if the main fails and all
    transactions are not fetched.

    :param subordinate: Reference to a subordinate (MySQL Server).
    :param main: Reference to the main (MySQL Server).
    :param timeout: Timeout for waiting for subordinate to catch up.
    """
    # Check servers for GTID support
    if not subordinate.gtid_enabled or not main.gtid_enabled:
        raise _errors.ProgrammingError(
            "Global Transaction IDs are not supported."
            )

    main_gtids = main.get_gtid_status()
    main_gtids = main_gtids[0].GTID_EXECUTED.strip(",")
    wait_for_subordinate_gtid(subordinate, main_gtids, timeout)

@_server.server_logging
def wait_for_subordinate_gtid(server, gtids, timeout=0):
    """Wait until a subordinate executes GITDs.

    The function `SELECT WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS` is called until the
    subordinate catches up. If the timeout period expires prior to achieving
    the condition the :class:`~mysql.fabric.errors.TimeoutError` exception is
    raised. If any thread is stopped, the
    :class:`~mysql.fabric.errors.DatabaseError` exception is raised.

    :param server: MySQL Server.
    :param gtids: Gtid information.
    :param timeout: Timeout for waiting for subordinate to catch up.
    """
    # Check servers for GTID support
    if not server.gtid_enabled:
        raise _errors.ProgrammingError(
            "Global Transaction IDs are not supported."
            )

    res = server.exec_stmt(_GTID_WAIT, {"params": (gtids, timeout)})

    if res is None or res[0] is None or res[0][0] is None:
        raise _errors.DatabaseError(
            "Error waiting for subordinate to catch up. "
            "GTID (%s)." % (gtids, )
        )
    elif res[0][0] == -1:
        raise _errors.TimeoutError(
            "Error waiting for subordinate to catch up. "
            "GTID (%s)." % (gtids, )
        )

    assert(res[0][0] > -1)

@_server.server_logging
def switch_main(subordinate, main, main_user, main_passwd=None,
                  from_beginning=True, main_log_file=None,
                  main_log_pos=None):
    """Switch subordinate to a new main by executing the `CHANGE MASTER` command.
    Look up the command in the MySQL Manual for further details.

    This method forms the `CHANGE MASTER` command based on the current
    settings of the subordinate along with the parameters provided and execute
    it. No prerequisites are checked.

    :param subordinate: Reference to a subordinate (MySQL Server).
    :param main: Reference to the main (MySQL Server).
    :param main_user: Replication user.
    :param main_passwd: Replication user password.
    :param from_beginning: If True, start from beginning of logged events.
    :param main_log_file: Main's log file (not needed for GTID).
    :param main_log_pos: main's log file position (not needed for GTID).
    """
    commands = []
    params = []
    main_host, main_port = split_host_port(
        main.address,
        MYSQL_DEFAULT_PORT
    )

    commands.append("MASTER_HOST = %s")
    params.append(main_host)
    commands.append("MASTER_PORT = %s")
    params.append(int(main_port))
    commands.append("MASTER_USER = %s")
    params.append(main_user)
    if main_passwd:
        commands.append("MASTER_PASSWORD = %s")
        params.append(main_passwd)
    else:
        commands.append("MASTER_PASSWORD = ''")

    if subordinate.gtid_enabled:
        commands.append("MASTER_AUTO_POSITION = 1")
    elif not from_beginning:
        commands.append("MASTER_LOG_FILE = %s")
        params.append(main_log_file)
        if main_log_pos >= 0:
            commands.append("MASTER_LOG_POS = %s" % main_log_pos)
            params.append(main_log_pos)

    subordinate.exec_stmt("CHANGE MASTER TO " + ", ".join(commands),
                    {"params": tuple(params)})

@_server.server_logging
def check_subordinate_issues(server):
    """Check subordinate's health.

    This method checks if the subordinate is setup correctly to operate in a
    replication environment and returns a dictionary that contains
    information on any issue found, if there is any. Specifically, it
    checks if the subordinate is alive and kicking and whether the `SQL_THREAD`
    and `IO_THREAD` are running or not.

    The dictionary returned may have the following keys::

      status["is_not_running"] = False
      status["is_not_configured"] = False
      status["io_not_running"] = False
      status["sql_not_running"] = False
      status["io_error"] = False
      status["sql_error"] = False

    :param server: MySQL Server.
    :return: Whether there is an issue or not and a dictionary with the
             issues, if there is any.
    """
    status = {
        'is_not_running': False,
        'is_not_configured': False,
        'io_not_running': False,
        'sql_not_running': False,
        'io_error': False,
        'sql_error': False
    }

    if not server.is_connected():
        status["is_not_running"] = True
        return True, status

    ret = get_subordinate_status(server)

    if not ret:
        status["is_not_configured"] = True
        return True, status

    if ret[0].Subordinate_IO_Running.upper() != "YES":
        status["io_not_running"] = True
    if ret[0].Subordinate_SQL_Running.upper() != "YES":
        status["sql_not_running"] = True
    if ret[0].Last_IO_Errno and ret[0].Last_IO_Errno > 0:
        status["io_error"] = ret[0].Last_IO_Error
    if ret[0].Last_SQL_Errno and ret[0].Last_SQL_Errno > 0:
        status["sql_error"] = ret[0].Last_SQL_Error

    error = not all([v is False for v in status.itervalues()])
    return error, status

@_server.server_logging
def check_subordinate_delay(subordinate, main):
    """Check subordinate's delay.

    It checks if both the main and subordinate are alive and kicking, whether
    the `SQL_THREAD` and `IO_THREAD` are running or not. It reports the
    `SQL_Delay`, `Seconds_Behind_Main` and finally if GTIDs are enabled
    the number of transactions behind main.

    The dictionary returned may have the following keys::

      status["is_not_running"] = False
      status["is_not_configured"] = False
      status["sql_delay"] = Value
      status["seconds_behind"] = Value
      status["gtids_behind"] = Value

    :param subordinate: Reference to a subordinate (MySQL Server).
    :param main: Reference to the main (MySQL Server).
    :return: A dictionary with delays, if there is any.
    """
    status = {
        'is_not_running': False,
        'is_not_configured': False,
        'sql_delay': 0,
        'seconds_behind': 0,
        'gtids_behind': 0
    }

    if not subordinate.is_connected() or not main.is_connected():
        status["is_not_running"] = True
        return status

    subordinate_status = get_subordinate_status(subordinate)

    if not subordinate_status:
        status["is_not_configured"] = True
        return status

    # Check if the subordinate must lag behind the main.
    sql_delay = subordinate_status[0].SQL_Delay
    if sql_delay:
        status["sql_delay"] = sql_delay

    # Check if the subordinate is lagging behind the main.
    seconds_behind = subordinate_status[0].Seconds_Behind_Main
    if seconds_behind:
        status["seconds_behind"] = seconds_behind

    # Check gtid trans behind.
    if subordinate.gtid_enabled:
        main_gtid_status = main.get_gtid_status()
        num_gtids_behind = get_subordinate_num_gtid_behind(subordinate,
                                                     main_gtid_status,
                                                     main.uuid)
        if num_gtids_behind:
            status["gtids_behind"] = num_gtids_behind

    return status

def _check_condition(server, threads, check_if_running):
    """Check if subordinate's threads are either running or stopped. If the
    `SQL_THREAD` or the `IO_THREAD` are stopped and there is an error,
    the :class:`~mysql.fabric.errors.DatabaseError` exception is raised.

    :param server: MySQL Server.
    :param threads: Which threads should be checked.
    :type threads: `SQL_THREAD` or `IO_THREAD`.
    :param check_if_running: If one should check whether threads are
                             running or stopped.
    :type check_if_running: Bool
    """
    if not threads:
        threads = (SQL_THREAD, IO_THREAD)
    assert(isinstance(threads, tuple))

    io_status = not check_if_running
    sql_status = not check_if_running
    check_stmt = "YES" if check_if_running else "NO"
    io_errno = sql_errno = 0
    io_error = sql_error = ""

    ret = get_subordinate_status(server)
    if ret:
        io_status = ret[0].Subordinate_IO_Running.upper() == check_stmt
        io_error = ret[0].Last_IO_Error
        io_errno = ret[0].Last_IO_Errno
        io_errno = io_errno if io_errno else 0

        sql_status = ret[0].Subordinate_SQL_Running.upper() == check_stmt
        sql_error = ret[0].Last_SQL_Error
        sql_errno = ret[0].Last_SQL_Errno
        sql_errno = sql_errno if sql_errno else 0

    achieved = True
    if SQL_THREAD in threads:
        achieved = achieved and sql_status
        if check_if_running and sql_errno != 0:
            raise _errors.DatabaseError(sql_error)

    if IO_THREAD in threads:
        achieved = achieved and io_status
        if check_if_running and io_errno != 0:
            raise _errors.DatabaseError(io_error)

    return achieved

def _check_status_condition(server, thread, status):
    """Check if a subordinate's thread has the requested status. If the `SQL_THREAD`
    or the `IO_THREAD` is stopped and there is an error, the following
    :class:`~mysql.fabric.errors.DatabaseError` exception is raised.

    :param server: MySQL Server.
    :param thread: Which thread should be checked.
    :type thread: `SQL_THREAD` or `IO_THREAD`.
    :param status: The status to be checked.
    """
    io_errno = sql_errno = 0
    io_error = sql_error = ""
    achieved = False

    ret = get_subordinate_status(server)
    if not ret:
        return achieved

    if SQL_THREAD == thread:
        sql_status = True if status in ret[0].Subordinate_SQL_Running_State else False
        sql_error = ret[0].Last_SQL_Error
        sql_errno = ret[0].Last_SQL_Errno

        if sql_errno and sql_errno != 0:
            raise _errors.DatabaseError(sql_error)

        achieved = sql_status

    elif IO_THREAD == thread:
        io_status = True if status in ret[0].Subordinate_IO_State else False
        io_error = ret[0].Last_IO_Error
        io_errno = ret[0].Last_IO_Errno

        if io_errno and io_errno != 0:
            raise _errors.DatabaseError(io_error)

        achieved = io_status

    return achieved


def synchronize_with_read_only(subordinate,  main, trnx_lag=0, timeout=5):
    """Synchronize the main with the subordinate. The function accepts a transaction
    lag and a timeout parameters.

    The transaction lag is used to determine that number of transactions the
    subordinate can lag behind the main before the main is locked to enable a
    complete sync.

    The timeout indicates the amount of time to wait for before taking a read
    lock on the main to enable a complete sync with the subordinate. The transaction
    lag alone is not enough to ensure that the subordinate catches up and at sometime
    we have to assume that the subordinate will not catch up and lock the source
    shard.

    :param subordinate: Reference to a subordinate (MySQL Server).
    :param main: Reference to the main (MySQL Server).
    :param trnx_lag: The number of transactions by which the subordinate can lag the
                                main before we can take a lock.
    :param timeout: The timeout for which we should wait before taking a
                               read lock on the main.
    """

    #Flag indicates if we are synced enough to take a read lock.
    synced = False

    #Syncing basically means that we either ensure that the subordinate
    #is "trnx_lag" transactions behind the main within the given
    #timeout. If the subordinate has managed to reach within "trnx_lag"
    #transactions we take a read lock and sync. We also take a read
    #lock and sync if the timeout has exceeded.
    while not synced:
        start_time = time.time()
        try:
            sync_subordinate_with_main(subordinate, main, timeout)
            main_gtids = main.get_gtid_status()
            if get_subordinate_num_gtid_behind(subordinate, main_gtids) <= trnx_lag:
                synced = True
            else:
                #Recalculate the amount of time left in the timeout, because
                #part of the time has already been used up when the code
                #reaches here.
                timeout = timeout - (time.time() - start_time)
                if timeout <= 0:
                    synced = True
        except _errors.TimeoutError:
            #If the code flow reaches here the timeout has been exceeded.
            #We lock the main and let the main and subordinate sync at this
            #point.
            break

    #At this point we lock the main and let the subordinate sync with the main.
    #This step is common across the entire algorithm. The preceeding steps
    #just help minimize the amount of time for which we take a read lock.
    main.read_only = True
    sync_subordinate_with_main(subordinate, main, timeout=0)
