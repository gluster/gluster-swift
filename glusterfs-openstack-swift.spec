%define _confdir %{_sysconfdir}/swift

# The following values are provided by passing the following arguments
# to rpmbuild.  For example:
#         --define "_version 1.0" --define "_release 1" --define "_name g4s"
#
%{!?_version:%define _version __PKG_VERSION__}
%{!?_name:%define _name __PKG_NAME__}
%{!?_release:%define _release __PKG_RELEASE__}

Summary  : GlusterFS Integration with OpenStack Object Storage (Swift).
Name     : %{_name}
Version  : %{_version}
Release  : %{_release}%{?dist}
Group    : Application/File
URL      : http://github.com/gluster/gluster-swift
Vendor   : Gluster Community
Source0  : %{_name}-%{_version}-%{_release}.tar.gz
License  : ASL 2.0
BuildArch: noarch
BuildRequires: python
BuildRequires: python-setuptools
Requires : memcached
Requires : openssl
Requires : python
Requires : python-prettytable
Requires : openstack-swift = 2.15.1
Requires : openstack-swift-account = 2.15.1
Requires : openstack-swift-container = 2.15.1
Requires : openstack-swift-object = 2.15.1
Requires : openstack-swift-proxy = 2.15.1
# gluster-swift has no hard-dependency on particular version of glusterfs
# so don't bump this up unless you want to force users to upgrade their
# glusterfs deployment
Requires : python-gluster >= 3.8.0
Obsoletes: glusterfs-swift-plugin
Obsoletes: glusterfs-swift
Obsoletes: glusterfs-ufo
Obsoletes: glusterfs-swift-container
Obsoletes: glusterfs-swift-object
Obsoletes: glusterfs-swift-proxy
Obsoletes: glusterfs-swift-account

%description
Gluster-Swift integrates GlusterFS as an alternative back end for OpenStack
Object Storage (Swift) leveraging the existing front end OpenStack Swift code.
Gluster volumes are used to store objects in files, containers are maintained
as top-level directories of volumes, where accounts are mapped one-to-one to
gluster volumes.

%prep
%setup -q -n gluster_swift-%{_version}

%build
%{__python} setup.py build

%install
rm -rf %{buildroot}

%{__python} setup.py install -O1 --skip-build --root %{buildroot}

mkdir -p      %{buildroot}/%{_confdir}/
cp -r etc/*   %{buildroot}/%{_confdir}/

# Man Pages
install -d -m 755 %{buildroot}%{_mandir}/man8
for page in doc/man/*.8; do
    install -p -m 0644 $page %{buildroot}%{_mandir}/man8
done

# Remove tests
%{__rm} -rf %{buildroot}/%{python_sitelib}/test

# Remove files provided by python-gluster (glusterfs-api earlier)
%{__rm} -rf %{buildroot}/%{python_sitelib}/gluster/__init__.p*

%files
%defattr(-,root,root)
%{python_sitelib}/gluster
%{python_sitelib}/gluster_swift-%{_version}*.egg-info
%{_bindir}/gluster-swift-gen-builders
%{_bindir}/gluster-swift-print-metadata
%{_bindir}/gluster-swift-migrate-metadata
%{_bindir}/gluster-swift-object-expirer
%{_bindir}/gswauth-add-account
%{_bindir}/gswauth-add-user
%{_bindir}/gswauth-cleanup-tokens
%{_bindir}/gswauth-delete-account
%{_bindir}/gswauth-delete-user
%{_bindir}/gswauth-list
%{_bindir}/gswauth-prep
%{_bindir}/gswauth-set-account-service
%{_mandir}/man8/*

%dir %{_confdir}
%config(noreplace) %{_confdir}/account-server.conf-gluster
%config(noreplace) %{_confdir}/container-server.conf-gluster
%config(noreplace) %{_confdir}/object-server.conf-gluster
%config(noreplace) %{_confdir}/swift.conf-gluster
%config(noreplace) %{_confdir}/proxy-server.conf-gluster
%config(noreplace) %{_confdir}/fs.conf-gluster
%config(noreplace) %{_confdir}/object-expirer.conf-gluster

%changelog
* Wed Nov 22 2017 Venkata R Edara <redara@redhat.com> - 2.15.1
- Rebase to Swift 2.15.1 (pike)

* Wed May 10 2017 Venkata R Edara <redara@redhat.com> - 2.10.1
- Rebase to Swift 2.10.1 (newton)

* Tue Mar 15 2016 Prashanth Pai <ppai@redhat.com> - 2.3.0-0
- Rebase to swift kilo (2.3.0)

* Fri May 23 2014 Thiago da Silva <thiago@redhat.com> - 1.13.1-1
- Update to Icehouse release

* Mon Oct 28 2013 Luis Pabon <lpabon@redhat.com> - 1.10.1-0
- Havana Release

* Wed Aug 21 2013 Luis Pabon <lpabon@redhat.com> - 1.8.0-7
- Update RPM spec file to support SRPMS
