import os, psycopg2, re, time, shutil
import logging

from urlparse import urlparse


logger = logging.getLogger(__name__)

class Postgresql:

    def __init__(self, config):
        self.name = config["name"]
        self.host, self.port = config["listen"].split(":")
        self.read_only_port = config.get('read_only_port', self.port)
        self.data_dir = config["data_dir"]
        self.replication = config["replication"]

        self.config = config

        self.cursor_holder = None

        # advertised connection for replication
        self.advertised_connection_string = "postgres://%s:%s@%s:%s/postgres" % (self.replication["username"], self.replication["password"], self.host, self.port)

        self.conn = None
        self.master = None

    def cursor(self):
        if not self.cursor_holder:
            local_connection_string = None

            # local connection for admin control and local reads
            if self.config.get('connect', None) == 'local':
                local_connection_string = "user=postgres port=%s" % self.server_port()
            else:
                local_connection_string = "postgres://%s:%s/postgres" % (self.host, self.server_port())

            logger.info("CONNECT: %s", local_connection_string)
            self.conn = psycopg2.connect(local_connection_string)
            self.conn.autocommit = True
            self.cursor_holder = self.conn.cursor()

        return self.cursor_holder

    def disconnect(self):
        try:
            self.conn.close()
        except Exception as e:
            logger.error("Error disconnecting: %s" % e)

    def query(self, sql):
        max_attempts = 0
        while True:
            try:
                self.cursor().execute(sql)
                break
            except psycopg2.OperationalError as e:
                if self.conn:
                    self.disconnect()
                self.cursor_holder = None
                if max_attempts > 4:
                    raise e
                max_attempts += 1
                time.sleep(5)
        return self.cursor()

    def data_directory_empty(self):
        return not os.path.exists(self.data_dir) or os.listdir(self.data_dir) == []

    def initialize(self):
        if os.system("initdb -D %s" % self.data_dir) == 0:
            #self.write_pg_hba()
            self.copy_pg_hba()

            self.start()
            self.create_replication_user()
            self.run_init_sql()
            self.stop()

            return True

        return False

    def sync_from_leader(self, leader):
        leader = urlparse(leader["address"])

        f = open("./pgpass", "w")
        f.write("%(hostname)s:%(port)s:*:%(username)s:%(password)s\n" %
                {"hostname": leader.hostname, "port": leader.port, "username": leader.username, "password": leader.password})
        f.close()

        os.system("chmod 600 pgpass")

        return os.system("PGPASSFILE=pgpass pg_basebackup -R -D %(data_dir)s --host=%(host)s --port=%(port)s -U %(username)s" %
                {"data_dir": self.data_dir, "host": leader.hostname, "port": leader.port, "username": leader.username}) == 0

    def is_leader(self):
        return not self.query("SELECT pg_is_in_recovery();").fetchone()[0]

    def is_running(self):
        return os.system("pg_ctl status -D %s > /dev/null" % self.data_dir) == 0

    def start(self, master=False):
        if self.is_running():
            logger.error("Cannot start PostgreSQL because one is already running.")
            return False

        pid_path = "%s/postmaster.pid" % self.data_dir
        if os.path.exists(pid_path):
            os.remove(pid_path)
            logger.info("Removed %s" % pid_path)

        self.master = master
        if master:
            logger.info("Starting PostgreSQL in Master mode")
        else:
            logger.info("Starting PostgreSQL in Slave mode")

        command_code = os.system("postgres -D %s %s &" % (self.data_dir, self.server_options()))
        while not self.is_running():
            time.sleep(5)
        return command_code != 0

    def stop(self):
        logger.info("Stopping PostgreSQL")
        return os.system("pg_ctl stop -w -D %s -m fast -w" % self.data_dir) != 0

    def reload(self):
        return os.system("pg_ctl reload -w -D %s" % self.data_dir) == 0

    def restart(self, master=False):
        self.master = master
        if master:
            logger.info("Restarting PostgreSQL in Master mode")
        else:
            logger.info("Restarting PostgreSQL in Slave mode")

        # return os.system("pg_ctl restart -m fast -w -D %s -o \"%s\"" % (self.data_dir, self.server_options())) == 0
        # return os.system("pg_ctl restart -w -D %s -o \"%s\" -m fast" % (self.data_dir, self.server_options())) == 0
        self.stop()  # fast shutdown
        self.start(master)

    def server_options(self):
        options = "-c listen_addresses=%s -c port=%s" % (self.host, self.server_port())
        for setting, value in self.config["parameters"].iteritems():
            options += " -c \"%s=%s\"" % (setting, value)
        return options

    def server_port(self):
        logger.info("MASTER: %s", self.master)
        return self.port if self.master else self.read_only_port

    def is_healthy(self):
        if not self.is_running():
            logger.warning("Postgresql is not running.")
            return False

        if self.is_leader():
            return True

        return True

    def is_healthiest_node(self, state_store):
        # this should only happen on initialization
        if state_store.last_leader_operation() is None:
            return True

        if (state_store.last_leader_operation() - self.xlog_position()) > self.config["maximum_lag_on_failover"]:
            return False

        for member in state_store.members():
            if member["hostname"] == self.name:
                continue
            try:
                member_conn = psycopg2.connect(member["address"])
                member_conn.autocommit = True
                member_cursor = member_conn.cursor()
                member_cursor.execute("SELECT %s - (pg_last_xlog_replay_location() - '0/000000'::pg_lsn) AS bytes;" % self.xlog_position())
                xlog_diff = member_cursor.fetchone()[0]
                logger.info([self.name, member["hostname"], xlog_diff])
                if xlog_diff < 0:
                    member_cursor.close()
                    return False
                member_cursor.close()
            except psycopg2.OperationalError:
                continue
        return True

    def replication_slot_name(self):
        member = os.environ.get("MEMBER")
        (member, _) = re.subn(r'[^a-z0-9]+', r'_', member)
        return member

    def write_pg_hba(self):
        f = open("%s/pg_hba.conf" % self.data_dir, "a")
        f.write("host replication %(username)s %(network)s md5" %
                {"username": self.replication["username"], "network": self.replication["network"]})
        f.close()

    def copy_pg_hba(self):
        if os.path.exists('pg_hba.conf'):
            logger.info("Copying pg_hba.conf file")
            shutil.copy2('pg_hba.conf', self.data_dir)
        else:
            logger.info("No pg_hba.conf file found - skipping")


    def write_recovery_conf(self, leader_hash):
        f = open("%s/recovery.conf" % self.data_dir, "w")
        f.write("""
standby_mode = 'on'
primary_slot_name = '%(recovery_slot)s'
recovery_target_timeline = 'latest'
""" % {"recovery_slot": self.name})
        if leader_hash is not None:
            leader = urlparse(leader_hash["address"])
            logger.info("Write Recovery Conf: %s:%s", leader.hostname, leader.port)
            f.write("""
primary_conninfo = 'user=%(user)s password=%(password)s host=%(hostname)s port=%(port)s sslmode=require sslcompression=1'
            """ % {"user": leader.username, "password": leader.password, "hostname": leader.hostname, "port": leader.port})

        if "recovery_conf" in self.config:
            for name, value in self.config["recovery_conf"].iteritems():
                f.write("%s = '%s'\n" % (name, value))
        f.close()

    def follow_the_leader(self, leader_hash):
        leader = urlparse(leader_hash["address"])
        if os.system("grep 'host=%(hostname)s port=%(port)s' %(data_dir)s/recovery.conf > /dev/null" % {"hostname": leader.hostname, "port": leader.port, "data_dir": self.data_dir}) != 0:
            self.write_recovery_conf(leader_hash)
            self.restart()
        return True

    def follow_no_leader(self):
        if not os.path.exists("%s/recovery.conf" % self.data_dir) or os.system("grep primary_conninfo %(data_dir)s/recovery.conf &> /dev/null" % {"data_dir": self.data_dir}) == 0:
            self.write_recovery_conf(None)
            if self.is_running():
                self.restart()
        return True

    def promote(self):
        self.stop()
        self.start(master=True)
        return os.system("pg_ctl promote -w -D %s" % self.data_dir) == 0
        # self.restart(master=True)

    def demote(self, leader):
        self.write_recovery_conf(leader)
        # self.restart()
        self.stop()
        self.start(master=False)

    def run_init_sql(self):
        if os.path.exists('init.sql'):
            logger.info("Running init.sql")
            sql_lines = open("init.sql", "r").read().split("\n")
            for sql in sql_lines:
                if not sql:
                    continue
                self.query(sql)

    def create_replication_user(self):
        #logger.info("Governor Starting Up: Running postgres single user mode to create repliaction user")
        #os.system("postgres --single -jE << CREATE USER '%s' WITH REPLICATION ENCRYPTED PASSWORD '%s';" % (self.replication["username"], self.replication["password"]))
        self.query("CREATE USER \"%s\" WITH REPLICATION ENCRYPTED PASSWORD '%s';" % (self.replication["username"], self.replication["password"]))

    def create_replication_slot(self, member):
        check = self.query("SELECT count(*) FROM pg_replication_slots WHERE slot_name = '%s';" % member).fetchone()[0]
        if not check:
            logging.info("Governor Running: Create Replication Slot: %s" % member)
            self.query("SELECT * FROM pg_create_physical_replication_slot('%s');" % member)

    def xlog_position(self):
        return self.query("SELECT pg_last_xlog_replay_location() - '0/0000000'::pg_lsn;").fetchone()[0]

    def last_operation(self):
        return self.query("SELECT pg_current_xlog_location() - '0/00000'::pg_lsn;").fetchone()[0]
