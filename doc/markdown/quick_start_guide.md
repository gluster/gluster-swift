# Quick Start Guide

## Contents
* [Overview](#overview)
* [Setting up GlusterFS](#gluster_setup)
* [Setting up gluster-swift](#swift_setup)
* [Using Gluster for Swift](#using_swift)
* [What now?](#what_now)

<a name="overview" />

## Overview
Gluster-swift project enables object based access (over Swift and S3 API)
to GlusterFS volumes.This guide is a great way to begin using gluster-swift,
and can be easily deployed on a single virtual machine. The final result
will be a single gluster-swift node.

The instructions provided in this guide has been tested specifically on:
* CentOS 7
* Ubuntu 16.04.2 LTS 'xenial'

If you are on other distributions, the steps related to where you fetch
the installation packages may vary.

> NOTE: In Gluster-Swift, accounts must be GlusterFS volumes.

<a name="gluster_setup" />

## Setting up GlusterFS

### Installing and starting GlusterFS

If on Ubuntu 16.04:

```sh
# add-apt-repository ppa:gluster/glusterfs-3.10
# apt-get install glusterfs-server attr
# service glusterfs-server start
```

If on CentOS 7:

```sh
# yum install centos-release-gluster
# yum install glusterfs-server
# service glusterd start
```

### Setting up a GlusterFS volume

You can use separate partitions as bricks. This guide illustrates using
loopback devices as bricks.

Create bricks:

```sh
# truncate -s 1GB /srv/disk{1..4}
# for i in `seq 1 4`;do mkfs.xfs -i size=512 /srv/disk$i ;done
# mkdir -p /export/brick{1..4}
```

Add the following lines to `/etc/fstab` to auto-mount the the bricks on system startup:

~~~
/srv/disk1    /export/brick1   xfs   loop,inode64,noatime,nodiratime 0 0
/srv/disk2    /export/brick2   xfs   loop,inode64,noatime,nodiratime 0 0
/srv/disk3    /export/brick3   xfs   loop,inode64,noatime,nodiratime 0 0
/srv/disk4    /export/brick4   xfs   loop,inode64,noatime,nodiratime 0 0
~~~

Mount the bricks:

```sh
# mount -a
```

You can now create and start the GlusterFS volume.
Make sure your hostname is in /etc/hosts or is DNS-resolvable.

```sh
# gluster volume create myvolume replica 2 transport tcp `hostname`:/export/brick{1..4}/data force
# gluster volume start myvolume
```

Mount the GlusterFS volume:

```sh
# mkdir -p /mnt/gluster-object/myvolume
# mount -t glusterfs `hostname`:myvolume /mnt/gluster-object/myvolume
```

<a name="swift_setup" />

## Setting up gluster-swift

### Installing Openstack Swift (newton version)

If on Ubuntu 16.04:

```sh
# apt install python-pip libffi-dev memcached
# git clone https://github.com/openstack/swift; cd swift
# git checkout -b release-2.10.1 tags/2.10.1
# pip install -r ./requirements.txt
# python setup.py install
```

If on CentOS 7:

```sh
# yum install centos-release-openstack-newton
# yum install openstack-swift-*
```

### Installing gluster-swift (newton version)

If on Ubuntu 16.04:

```sh
# git clone https://github.com/gluster/gluster-swift; cd gluster-swift
# pip install -r ./requirements.txt
# python setup.py install
```

If on CentoOS 7:

```sh
# yum install epel-release
# yum install python-scandir python-prettytable git
# git clone https://github.com/gluster/gluster-swift; cd gluster-swift
# python setup.py install
```

### gluster-swift configuration files
As with OpenStack Swift, gluster-swift uses `/etc/swift` as the
directory containing the configuration files.  You will need to base
the configuration files on the template files provided.  On new
installations, the simplest way is to copy the `*.conf-gluster`
files to `*.conf` files as follows:

Copy conf files from `etc` directory in gluster-swift repo to
`/etc/swift` and rename the template files.

```sh
# mkdir -p /etc/swift/
# cp etc/* /etc/swift/
# cd /etc/swift
# for tmpl in *.conf-gluster ; do cp ${tmpl} ${tmpl%.*}.conf; done
```

### Export GlusterFS volumes over gluster-swift

You now need to generate the ring files, which informs gluster-swift
which GlusterFS volumes are accessible over the object storage interface.
The format is:

```sh
# gluster-swift-gen-builders [VOLUME] [VOLUME...]
```

Where *VOLUME* is the name of the GlusterFS volume which you would
like to access over gluster-swift.

Let's now expose the GlusterFS volume called `myvolume` you created above
by executing the following command:

```sh
# gluster-swift-gen-builders myvolume
```

### Start gluster-swift
Use the following commands to start gluster-swift:

```sh
# swift-init main start
```

<a name="using_swift" />

## Using gluster-swift

### Create a container
Create a container using the following command:

```sh
# curl -i -X PUT http://localhost:8080/v1/AUTH_myvolume/mycontainer
```

It should return `HTTP/1.1 201 Created` on a successful creation. You can
also confirm that the container has been created by inspecting the GlusterFS
volume:

```sh
# ls /mnt/gluster-object/myvolume
```

#### Create an object
You can now place an object in the container you have just created:

```sh
# echo "Hello World" > mytestfile
# curl -i -X PUT -T mytestfile http://localhost:8080/v1/AUTH_myvolume/mycontainer/mytestfile
```

To confirm that the object has been written correctly, you can compare the
test file with the object you created:

```sh
# cat /mnt/gluster-object/myvolume/mycontainer/mytestfile
```

#### Request the object
Now you can retreive the object and inspect its contents using the
following commands:

```sh
# curl -i -X GET -o newfile http://localhost:8080/v1/AUTH_myvolume/mycontainer/mytestfile
# cat newfile
```

<a name="what_now" />

## What now?
For more information, please visit the following links:

* [Authentication Services Start Guide][]
* [GlusterFS Quick Start Guide][]
* [OpenStack Swift API][]
* [Accessing over Amazon S3 API][]


[GlusterFS Quick Start Guide]: http://www.gluster.org/community/documentation/index.php/QuickStart
[OpenStack Swift API]: http://docs.openstack.org/api/openstack-object-storage/1.0/content/
[Authentication Services Start Guide]: auth_guide.md
[Accessing over Amazon S3 API]: s3.md
