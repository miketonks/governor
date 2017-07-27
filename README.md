# Governor: A Template for PostgreSQL HA with etcd

Docker based Postgres Cluster: Streaming Replication with automatic setup out of the box, batteries *included*.

Automatic Failover.  Zero Downtime Upgrades.  Configurable via ENV variables, with etcd cluster for master election as external service.  Production ready, stable, and fully dev-ops approved.

This is a fork of https://github.com/compose/governor with a few improvements to make it cluster safe and production ready.

## USAGE

Set up three docker hosts,

 - server1: 192.168.42.1
 - server2: 192.168.42.2
 - server3: 192.168.42.3

Let's run a single etcd node for now:

docker -H tcp://server1:2375 run -d --name etcd1 --net host coreos/etcd -addr 192.168.42.1:4001 -peer-addr 192.168.42.1:4002

Now let's run up three governor nodes:

```
docker -H tcp://server1:2375 run -d --name pg1 --net host \
  -e GOVERNOR_ETCD_HOST=192.168.42.1:4001 \
  -e GOVERNOR_POSTGRESQL_NAME=postgresql1 \
  -e GOVERNOR_POSTGRESQL_LISTEN=192.168.42.1:5432 \
  -e GOVERNOR_POSTGRESQL_DATA_DIR=/data/postgres \
  -e GOVERNOR_POSTGRESQL_REPLICATION_NETWORK=192.168.42.1/24 miketonks/governor

  docker -H tcp://server2:2375 run -d --name pg2 --net host \
    -e GOVERNOR_ETCD_HOST=192.168.42.1:4001 \
    -e GOVERNOR_POSTGRESQL_NAME=postgresql2 \
    -e GOVERNOR_POSTGRESQL_LISTEN=192.168.42.2:5432 \
    -e GOVERNOR_POSTGRESQL_DATA_DIR=/data/postgres \
    -e GOVERNOR_POSTGRESQL_REPLICATION_NETWORK=192.168.42.1/24 miketonks/governor

    docker -H tcp://server3:2375 run -d --name pg3 --net host \
      -e GOVERNOR_ETCD_HOST=192.168.42.1:4001 \
      -e GOVERNOR_POSTGRESQL_NAME=postgresql3 \
      -e GOVERNOR_POSTGRESQL_LISTEN=192.168.42.3:5432 \
      -e GOVERNOR_POSTGRESQL_DATA_DIR=/data/postgres \
      -e GOVERNOR_POSTGRESQL_REPLICATION_NETWORK=192.168.42.1/24 miketonks/governor
```

The first node started will assume master role, and the other two will become slaves.

```
$docker logs pg1

2015-07-10 16:10:32,404 INFO: Governor Starting up: Starting Postgres
2015-07-10 16:10:34,460 INFO: Governor Running: Starting Running Loop
2015-07-10 16:10:39,474 INFO: Lock owner: postgresql1; I am postgresql1
2015-07-10 16:10:39,476 INFO: Governor Running: no action.  i am the leader with the lock
2015-07-10 16:10:39,476 INFO: Governor Running: I am the Leader
2015-07-10 16:10:39,477 INFO: Governor Running: Create Replication Slot: postgresql2
2015-07-10 16:10:49,495 INFO: Lock owner: postgresql1; I am postgresql1
2015-07-10 16:10:49,497 INFO: Governor Running: no action.  i am the leader with the lock
2015-07-10 16:10:49,497 INFO: Governor Running: I am the Leader

$docker logs pg2

2015-07-10 16:10:32,404 INFO: Governor Starting up: Starting Postgres
2015-07-10 16:10:32,416 INFO: Governor Running: Starting Running Loop
FATAL:  the database system is starting up
LOG:  started streaming WAL from primary at 0/3000000 on timeline 1
LOG:  redo starts at 0/3000028
LOG:  consistent recovery state reached at 0/30000F0
LOG:  database system is ready to accept read only connections
2015-07-10 16:10:52,461 INFO: Lock owner: postgresql1; I am postgresql2
2015-07-10 16:10:52,461 INFO: does not have lock
2015-07-10 16:10:52,465 INFO: Governor Running: no action.  i am a secondary and i am following a leader
```

Now kill the pg1 node and you will see, after a short while, that pg2 (or pg3)  automatically reconfigures and takes over as primary

Bring pg1 back online and it will rejoin the cluster as a slave.
