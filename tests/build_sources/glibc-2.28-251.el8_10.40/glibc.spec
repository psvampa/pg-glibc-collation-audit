%define glibcsrcdir glibc-2.28
%define glibcversion 2.28
%define glibcrelease 251%{?dist}
# Pre-release tarballs are pulled in from git using a command that is
# effectively:
#
# git archive HEAD --format=tar --prefix=$(git describe --match 'glibc-*')/ \
#	> $(git describe --match 'glibc-*').tar
# gzip -9 $(git describe --match 'glibc-*').tar
#
# glibc_release_url is only defined when we have a release tarball.
%{lua: if string.match(rpm.expand("%glibcsrcdir"), "^glibc%-[0-9.]+$") then
  rpm.define("glibc_release_url https://ftp.gnu.org/gnu/glibc/") end}
##############################################################################
# We support the following options:
# --with/--without,
# * testsuite - Running the testsuite.
# * benchtests - Running and building benchmark subpackage.
# * bootstrap - Bootstrapping the package.
# * werror - Build with -Werror
# * docs - Build with documentation and the required dependencies.
# * valgrind - Run smoke tests with valgrind to verify dynamic loader.
#
# You must always run the testsuite for production builds.
# Default: Always run the testsuite.
%bcond_without testsuite
# Default: Always build the benchtests.
%bcond_without benchtests
# Default: Not bootstrapping.
%bcond_with bootstrap
# Default: Enable using -Werror
%bcond_without werror
# Default: Always build documentation.
%bcond_without docs

# Default: Always run valgrind tests if there is architecture support.
%ifarch %{valgrind_arches}
%bcond_without valgrind
%else
%bcond_with valgrind
%endif
# Restrict %%{valgrind_arches} further in case there are problems with
# the smoke test.
%if %{with valgrind}
%ifarch ppc64 ppc64p7
# The valgrind smoke test does not work on ppc64, ppc64p7 (bug 1273103).
%undefine with_valgrind
%endif
%endif

%if %{with bootstrap}
# Disable benchtests, -Werror, docs, and valgrind if we're bootstrapping
%undefine with_benchtests
%undefine with_werror
%undefine with_docs
%undefine with_valgrind
%endif

# Only some architectures have static PIE support.
%define pie_arches %{ix86} x86_64

# Build the POWER9 runtime on POWER, but only for downstream.
%ifarch ppc64le
%define buildpower9 0%{?rhel} > 0
%else
%define buildpower9 0
%endif

# RHEL 8 does not have a working %%dnl macro.
%define comment() %{nil}

##############################################################################
# Utility functions for pre/post scripts.  Stick them at the beginning of
# any lua %pre, %post, %postun, etc. sections to have them expand into
# those scripts.  It only works in lua sections and not anywhere else.
%define glibc_post_funcs() \
-- We use lua posix.exec because there may be no shell that we can \
-- run during glibc upgrade.  We used to implement much of %%post as a \
-- C program, but from an overall maintenance perspective the lua in \
-- the spec file was simpler and safer given the operations required. \
-- All lua code will be ignored by rpm-ostree; see: \
-- https://github.com/projectatomic/rpm-ostree/pull/1869 \
-- If we add new lua actions to the %%post code we should coordinate \
-- with rpm-ostree and ensure that their glibc install is functional. \
function post_exec (program, ...) \
  local pid = posix.fork () \
  if pid == 0 then \
    posix.exec (program, ...) \
    assert (nil) \
  elseif pid > 0 then \
    posix.wait (pid) \
  end \
end \
\
function update_gconv_modules_cache () \
  local iconv_dir = "%{_libdir}/gconv" \
  local iconv_cache = iconv_dir .. "/gconv-modules.cache" \
  local iconv_modules = iconv_dir .. "/gconv-modules" \
  if (posix.utime (iconv_modules) == 0) then \
    if (posix.utime (iconv_cache) == 0) then \
      post_exec ("%{_prefix}/sbin/iconvconfig", \
		 "-o", iconv_cache, \
		 "--nostdlib", \
		 iconv_dir) \
    else \
      io.stdout:write ("Error: Missing " .. iconv_cache .. " file.\n") \
    end \
  end \
end \
%{nil}

##############################################################################
# %%package glibc - The GNU C Library (glibc) core package.
##############################################################################
Summary: The GNU libc libraries
Name: glibc
Version: %{glibcversion}
Release: %{glibcrelease}.40

# In general, GPLv2+ is used by programs, LGPLv2+ is used for
# libraries.
#
# LGPLv2+ with exceptions is used for things that are linked directly
# into dynamically linked programs and shared libraries (e.g. crt
# files, lib*_nonshared.a).  Historically, this exception also applies
# to parts of libio.
#
# GPLv2+ with exceptions is used for parts of the Arm unwinder.
#
# GFDL is used for the documentation.
#
# Some other licenses are used in various places (BSD, Inner-Net,
# ISC, Public Domain).
#
# HSRL and FSFAP are only used in test cases, which currently do not
# ship in binary RPMs, so they are not listed here.  MIT is used for
# scripts/install-sh, which does not ship, either.
#
# GPLv3+ is used by manual/texinfo.tex, which we do not use.
#
# LGPLv3+ is used by some Hurd code, which we do not build.
#
# LGPLv2 is used in one place (time/timespec_get.c, by mistake), but
# it is not actually compiled, so it does not matter for libraries.
License: LGPLv2+ and LGPLv2+ with exceptions and GPLv2+ and GPLv2+ with exceptions and BSD and Inner-Net and ISC and Public Domain and GFDL

URL: http://www.gnu.org/software/glibc/
Source0: %{?glibc_release_url}%{glibcsrcdir}.tar.xz
Source1: build-locale-archive.c
Source4: nscd.conf
Source8: power6emul.c
Source9: bench.mk
Source10: glibc-bench-compare
# A copy of localedata/SUPPORTED in the Source0 tarball.  The
# SUPPORTED file is used below to generate the list of locale
# packages, using a Lua snippet.
Source11: SUPPORTED

# Include in the source RPM for reference.
Source12: ChangeLog.old

Source13: wrap-find-debuginfo.sh

######################################################################
# Activate the wrapper script for debuginfo generation, by rewriting
# the definition of __debug_install_post.
%{lua:
local wrapper = rpm.expand("%{SOURCE13}")
local sysroot = rpm.expand("%{glibc_sysroot}")
local original = rpm.expand("%{__find_debuginfo}")
rpm.define("__find_debuginfo  " .. wrapper .. " " .. sysroot .. " " .. original)
}

# The wrapper script relies on the fact that debugedit does not change
# build IDs.
%define _no_recompute_build_ids 1
%undefine _unique_build_ids

# glibc_ldso: ABI-specific program interpreter name.  Used for debuginfo
# extraction (wrap-find-debuginfo.sh) and smoke testing ($run_ldso below).
# glibc_has_libnldbl: libnldbl_nonshared.a is built and installed.
# glibc_has_libmvec: libmvec is built and installed.
%ifarch %{ix86}
%global glibc_ldso /lib/ld-linux.so.2
%global glibc_has_libnldbl 0
%global glibc_has_libmvec 0
%endif
%ifarch aarch64
%global glibc_ldso /lib/ld-linux-aarch64.so.1
%global glibc_has_libnldbl 0
%global glibc_has_libmvec 0
%endif
%ifarch ppc
%global glibc_ldso /lib/ld.so.1
%global glibc_has_libnldbl 1
%global glibc_has_libmvec 0
%endif
%ifarch ppc64
%global glibc_ldso /lib64/ld64.so.1
%global glibc_has_libnldbl 1
%global glibc_has_libmvec 0
%endif
%ifarch ppc64le
%global glibc_ldso /lib64/ld64.so.2
%global glibc_has_libnldbl 1
%global glibc_has_libmvec 0
%endif
%ifarch riscv64
%global glibc_ldso /lib/ld-linux-riscv64-lp64d.so.1
%global glibc_has_libnldbl 0
%global glibc_has_libmvec 0
%endif
%ifarch s390
%global glibc_ldso /lib/ld.so.1
%global glibc_has_libnldbl 0
%global glibc_has_libmvec 0
%endif
%ifarch s390x
%global glibc_ldso /lib/ld64.so.1
%global glibc_has_libnldbl 1
%global glibc_has_libmvec 0
%endif
%ifarch x86_64 x86_64_v2 x86_64_v3 x86_64_v4
%global glibc_ldso /lib64/ld-linux-x86-64.so.2
%global glibc_has_libnldbl 0
%global glibc_has_libmvec 1
%endif

# This is necessary to enable source RPM building under noarch, as
# used by some build environments.
%ifarch noarch
%global glibc_ldso /lib/ld.so
%global glibc_has_libnldbl 0
%global glibc_has_libmvec 0
%endif

# If the architecture places the official ld.so name under /lib,
# but we use /lib64, we need to install both files.
%if "%{_lib}" == "lib64" && "%{dirname:%{glibc_ldso}}" == "/lib"
%global glibc_ldso_alternate %{_libdir}/%{basename:%{glibc_ldso}}
%endif

##############################################################################
# Patches:
# - See each individual patch file for origin and upstream status.
# - For new patches follow template.patch format.
##############################################################################
Patch2: glibc-fedora-nscd.patch
Patch3: glibc-rh697421.patch
Patch4: glibc-fedora-linux-tcsetattr.patch
Patch5: glibc-rh741105.patch
Patch6: glibc-fedora-localedef.patch
Patch7: glibc-fedora-nis-rh188246.patch
Patch8: glibc-fedora-manual-dircategory.patch
Patch9: glibc-rh827510.patch
Patch10: glibc-fedora-locarchive.patch
Patch11: glibc-fedora-streams-rh436349.patch
Patch12: glibc-rh819430.patch
Patch13: glibc-fedora-localedata-rh61908.patch
Patch14: glibc-fedora-__libc_multiple_libcs.patch
Patch15: glibc-rh1070416.patch
Patch16: glibc-nscd-sysconfig.patch
Patch17: glibc-cs-path.patch
Patch18: glibc-c-utf8-locale.patch
Patch23: glibc-python3.patch
Patch24: glibc-with-nonshared-cflags.patch
Patch25: glibc-asflags.patch
Patch27: glibc-rh1614253.patch
Patch28: glibc-rh1577365.patch
Patch29: glibc-rh1615781.patch
Patch30: glibc-rh1615784.patch
Patch31: glibc-rh1615790.patch
Patch32: glibc-rh1622675.patch
Patch33: glibc-rh1622678-1.patch
Patch34: glibc-rh1622678-2.patch
Patch35: glibc-rh1631293-1.patch
Patch36: glibc-rh1631293-2.patch
Patch37: glibc-rh1623536.patch
Patch38: glibc-rh1631722.patch
Patch39: glibc-rh1631730.patch
Patch40: glibc-rh1623536-2.patch
Patch41: glibc-rh1614979.patch
Patch42: glibc-rh1645593.patch
Patch43: glibc-rh1645596.patch
Patch44: glibc-rh1645604.patch
Patch45: glibc-rh1646379.patch
Patch46: glibc-rh1645601.patch
Patch52: glibc-rh1638523-1.patch
Patch47: glibc-rh1638523-2.patch
Patch48: glibc-rh1638523-3.patch
Patch49: glibc-rh1638523-4.patch
Patch50: glibc-rh1638523-5.patch
Patch51: glibc-rh1638523-6.patch
Patch53: glibc-rh1641982.patch
Patch54: glibc-rh1645597.patch
Patch55: glibc-rh1650560-1.patch
Patch56: glibc-rh1650560-2.patch
Patch57: glibc-rh1650563.patch
Patch58: glibc-rh1650566.patch
Patch59: glibc-rh1650571.patch
Patch60: glibc-rh1638520.patch
Patch61: glibc-rh1651274.patch
Patch62: glibc-rh1654010-1.patch
Patch63: glibc-rh1635779.patch
Patch64: glibc-rh1654010-2.patch
Patch65: glibc-rh1654010-3.patch
Patch66: glibc-rh1654010-4.patch
Patch67: glibc-rh1654010-5.patch
Patch68: glibc-rh1654010-6.patch
Patch69: glibc-rh1642094-1.patch
Patch70: glibc-rh1642094-2.patch
Patch71: glibc-rh1642094-3.patch
Patch72: glibc-rh1654872-1.patch
Patch73: glibc-rh1654872-2.patch
Patch74: glibc-rh1651283-1.patch
Patch75: glibc-rh1662843-1.patch
Patch76: glibc-rh1662843-2.patch
Patch77: glibc-rh1623537.patch
Patch78: glibc-rh1577438.patch
Patch79: glibc-rh1664408.patch
Patch80: glibc-rh1651742.patch
Patch81: glibc-rh1672773.patch
Patch82: glibc-rh1651283-2.patch
Patch83: glibc-rh1651283-3.patch
Patch84: glibc-rh1651283-4.patch
Patch85: glibc-rh1651283-5.patch
Patch86: glibc-rh1651283-6.patch
Patch87: glibc-rh1651283-7.patch
Patch88: glibc-rh1659293-1.patch
Patch89: glibc-rh1659293-2.patch
Patch90: glibc-rh1639343-1.patch
Patch91: glibc-rh1639343-2.patch
Patch92: glibc-rh1639343-3.patch
Patch93: glibc-rh1639343-4.patch
Patch94: glibc-rh1639343-5.patch
Patch95: glibc-rh1639343-6.patch
Patch96: glibc-rh1663035.patch
Patch97: glibc-rh1658901.patch
Patch98: glibc-rh1659512-1.patch
Patch99: glibc-rh1659512-2.patch
Patch100: glibc-rh1659438-1.patch
Patch101: glibc-rh1659438-2.patch
Patch102: glibc-rh1659438-3.patch
Patch103: glibc-rh1659438-4.patch
Patch104: glibc-rh1659438-5.patch
Patch105: glibc-rh1659438-6.patch
Patch106: glibc-rh1659438-7.patch
Patch107: glibc-rh1659438-8.patch
Patch108: glibc-rh1659438-9.patch
Patch109: glibc-rh1659438-10.patch
Patch110: glibc-rh1659438-11.patch
Patch111: glibc-rh1659438-12.patch
Patch112: glibc-rh1659438-13.patch
Patch113: glibc-rh1659438-14.patch
Patch114: glibc-rh1659438-15.patch
Patch115: glibc-rh1659438-16.patch
Patch116: glibc-rh1659438-17.patch
Patch117: glibc-rh1659438-18.patch
Patch118: glibc-rh1659438-19.patch
Patch119: glibc-rh1659438-20.patch
Patch120: glibc-rh1659438-21.patch
Patch121: glibc-rh1659438-22.patch
Patch122: glibc-rh1659438-23.patch
Patch123: glibc-rh1659438-24.patch
Patch124: glibc-rh1659438-25.patch
Patch125: glibc-rh1659438-26.patch
Patch126: glibc-rh1659438-27.patch
Patch127: glibc-rh1659438-28.patch
Patch128: glibc-rh1659438-29.patch
Patch129: glibc-rh1659438-30.patch
Patch130: glibc-rh1659438-31.patch
Patch131: glibc-rh1659438-32.patch
Patch132: glibc-rh1659438-33.patch
Patch133: glibc-rh1659438-34.patch
Patch134: glibc-rh1659438-35.patch
Patch135: glibc-rh1659438-36.patch
Patch136: glibc-rh1659438-37.patch
Patch137: glibc-rh1659438-38.patch
Patch138: glibc-rh1659438-39.patch
Patch139: glibc-rh1659438-40.patch
Patch140: glibc-rh1659438-41.patch
Patch141: glibc-rh1659438-42.patch
Patch142: glibc-rh1659438-43.patch
Patch143: glibc-rh1659438-44.patch
Patch144: glibc-rh1659438-45.patch
Patch145: glibc-rh1659438-46.patch
Patch146: glibc-rh1659438-47.patch
Patch147: glibc-rh1659438-48.patch
Patch148: glibc-rh1659438-49.patch
Patch149: glibc-rh1659438-50.patch
Patch150: glibc-rh1659438-51.patch
Patch151: glibc-rh1659438-52.patch
Patch152: glibc-rh1659438-53.patch
Patch153: glibc-rh1659438-54.patch
Patch154: glibc-rh1659438-55.patch
Patch155: glibc-rh1659438-56.patch
Patch156: glibc-rh1659438-57.patch
Patch157: glibc-rh1659438-58.patch
Patch158: glibc-rh1659438-59.patch
Patch159: glibc-rh1659438-60.patch
Patch160: glibc-rh1659438-61.patch
Patch161: glibc-rh1659438-62.patch
Patch162: glibc-rh1702539-1.patch
Patch163: glibc-rh1702539-2.patch
Patch164: glibc-rh1701605-1.patch
Patch165: glibc-rh1701605-2.patch
Patch166: glibc-rh1691528-1.patch
Patch167: glibc-rh1691528-2.patch
Patch168: glibc-rh1706777.patch
Patch169: glibc-rh1710478.patch
Patch170: glibc-rh1670043-1.patch
Patch171: glibc-rh1670043-2.patch
Patch172: glibc-rh1710894.patch
Patch173: glibc-rh1699194-1.patch
Patch174: glibc-rh1699194-2.patch
Patch175: glibc-rh1699194-3.patch
Patch176: glibc-rh1699194-4.patch
Patch177: glibc-rh1727241-1.patch
Patch178: glibc-rh1727241-2.patch
Patch179: glibc-rh1727241-3.patch
Patch180: glibc-rh1717438.patch
Patch181: glibc-rh1727152.patch
Patch182: glibc-rh1724975.patch
Patch183: glibc-rh1722215.patch
Patch184: glibc-rh1764234-1.patch
Patch185: glibc-rh1764234-2.patch
Patch186: glibc-rh1764234-3.patch
Patch187: glibc-rh1764234-4.patch
Patch188: glibc-rh1764234-5.patch
Patch189: glibc-rh1764234-6.patch
Patch190: glibc-rh1764234-7.patch
Patch191: glibc-rh1764234-8.patch
Patch192: glibc-rh1747505-1.patch
Patch193: glibc-rh1747505-2.patch
Patch194: glibc-rh1747505-3.patch
Patch195: glibc-rh1747505-4.patch
Patch196: glibc-rh1747453.patch
Patch197: glibc-rh1764241.patch
Patch198: glibc-rh1746933-1.patch
Patch199: glibc-rh1746933-2.patch
Patch200: glibc-rh1746933-3.patch
Patch201: glibc-rh1735747-1.patch
Patch202: glibc-rh1735747-2.patch
Patch203: glibc-rh1764226-1.patch
Patch204: glibc-rh1764226-2.patch
Patch205: glibc-rh1764226-3.patch
Patch206: glibc-rh1764218-1.patch
Patch207: glibc-rh1764218-2.patch
Patch208: glibc-rh1764218-3.patch
Patch209: glibc-rh1682954.patch
Patch210: glibc-rh1746928.patch
Patch211: glibc-rh1747502.patch
Patch212: glibc-rh1747502-1.patch
Patch213: glibc-rh1747502-2.patch
Patch214: glibc-rh1747502-3.patch
Patch215: glibc-rh1747502-4.patch
Patch216: glibc-rh1747502-5.patch
Patch217: glibc-rh1747502-6.patch
Patch218: glibc-rh1747502-7.patch
Patch219: glibc-rh1747502-8.patch
Patch220: glibc-rh1747502-9.patch
Patch221: glibc-rh1726638-1.patch
Patch222: glibc-rh1726638-2.patch
Patch223: glibc-rh1726638-3.patch
Patch224: glibc-rh1764238-1.patch
Patch225: glibc-rh1764238-2.patch
Patch226: glibc-rh1764242.patch
Patch227: glibc-rh1769304.patch
Patch228: glibc-rh1749439-1.patch
Patch229: glibc-rh1749439-2.patch
Patch230: glibc-rh1749439-3.patch
Patch231: glibc-rh1749439-4.patch
Patch232: glibc-rh1749439-5.patch
Patch233: glibc-rh1749439-6.patch
Patch234: glibc-rh1749439-7.patch
Patch235: glibc-rh1749439-8.patch
Patch236: glibc-rh1749439-9.patch
Patch237: glibc-rh1749439-10.patch
Patch238: glibc-rh1749439-11.patch
Patch239: glibc-rh1749439-12.patch
Patch240: glibc-rh1749439-13.patch
Patch241: glibc-rh1764231-1.patch
Patch242: glibc-rh1764231-2.patch
Patch243: glibc-rh1764235.patch
Patch244: glibc-rh1361965.patch
Patch245: glibc-rh1764223.patch
Patch246: glibc-rh1764214.patch
Patch247: glibc-rh1774021.patch
Patch248: glibc-rh1775294.patch
Patch249: glibc-rh1777241.patch
Patch250: glibc-rh1410154-1.patch
Patch251: glibc-rh1410154-2.patch
Patch252: glibc-rh1410154-3.patch
Patch253: glibc-rh1410154-4.patch
Patch254: glibc-rh1410154-5.patch
Patch255: glibc-rh1410154-6.patch
Patch256: glibc-rh1410154-7.patch
Patch257: glibc-rh1410154-8.patch
Patch258: glibc-rh1410154-9.patch
Patch259: glibc-rh1410154-10.patch
Patch260: glibc-rh1410154-11.patch
Patch261: glibc-rh1410154-12.patch
Patch262: glibc-rh1410154-13.patch
Patch263: glibc-rh1410154-14.patch
Patch264: glibc-rh1410154-15.patch
Patch265: glibc-rh1410154-16.patch
Patch266: glibc-rh1810142-1.patch
Patch267: glibc-rh1810142-2.patch
Patch268: glibc-rh1810142-3.patch
Patch269: glibc-rh1810142-4.patch
Patch270: glibc-rh1810142-5.patch
Patch271: glibc-rh1810142-6.patch
Patch272: glibc-rh1743445-1.patch
Patch273: glibc-rh1743445-2.patch
Patch274: glibc-rh1780204-01.patch
Patch275: glibc-rh1780204-02.patch
Patch276: glibc-rh1780204-03.patch
Patch277: glibc-rh1780204-04.patch
Patch278: glibc-rh1780204-05.patch
Patch279: glibc-rh1780204-06.patch
Patch280: glibc-rh1780204-07.patch
Patch281: glibc-rh1780204-08.patch
Patch282: glibc-rh1780204-09.patch
Patch283: glibc-rh1780204-10.patch
Patch284: glibc-rh1780204-11.patch
Patch285: glibc-rh1780204-12.patch
Patch286: glibc-rh1780204-13.patch
Patch287: glibc-rh1780204-14.patch
Patch288: glibc-rh1780204-15.patch
Patch289: glibc-rh1780204-16.patch
Patch290: glibc-rh1780204-17.patch
Patch291: glibc-rh1780204-18.patch
Patch292: glibc-rh1780204-19.patch
Patch293: glibc-rh1780204-20.patch
Patch294: glibc-rh1780204-21.patch
Patch295: glibc-rh1780204-22.patch
Patch296: glibc-rh1780204-23.patch
Patch297: glibc-rh1780204-24.patch
Patch298: glibc-rh1780204-25.patch
Patch299: glibc-rh1780204-26.patch
Patch300: glibc-rh1780204-27.patch
Patch301: glibc-rh1780204-28.patch
Patch302: glibc-rh1784519.patch
Patch303: glibc-rh1775819.patch
Patch304: glibc-rh1774114.patch
Patch305: glibc-rh1812756-1.patch
Patch306: glibc-rh1812756-2.patch
Patch307: glibc-rh1812756-3.patch
Patch308: glibc-rh1757354.patch
Patch309: glibc-rh1784520.patch
Patch310: glibc-rh1784525.patch
Patch311: glibc-rh1810146.patch
Patch312: glibc-rh1810223-1.patch
Patch313: glibc-rh1810223-2.patch
Patch314: glibc-rh1811796-1.patch
Patch315: glibc-rh1811796-2.patch
Patch316: glibc-rh1813398.patch
Patch317: glibc-rh1813399.patch
Patch318: glibc-rh1810224-1.patch
Patch319: glibc-rh1810224-2.patch
Patch320: glibc-rh1810224-3.patch
Patch321: glibc-rh1810224-4.patch
Patch322: glibc-rh1783303-1.patch
Patch323: glibc-rh1783303-2.patch
Patch324: glibc-rh1783303-3.patch
Patch325: glibc-rh1783303-4.patch
Patch326: glibc-rh1783303-5.patch
Patch327: glibc-rh1783303-6.patch
Patch328: glibc-rh1783303-7.patch
Patch329: glibc-rh1783303-8.patch
Patch330: glibc-rh1783303-9.patch
Patch331: glibc-rh1783303-10.patch
Patch332: glibc-rh1783303-11.patch
Patch333: glibc-rh1783303-12.patch
Patch334: glibc-rh1783303-13.patch
Patch335: glibc-rh1783303-14.patch
Patch336: glibc-rh1783303-15.patch
Patch337: glibc-rh1783303-16.patch
Patch338: glibc-rh1783303-17.patch
Patch339: glibc-rh1783303-18.patch
Patch340: glibc-rh1642150-1.patch
Patch341: glibc-rh1642150-2.patch
Patch342: glibc-rh1642150-3.patch
Patch343: glibc-rh1774115.patch
Patch344: glibc-rh1780204-29.patch
Patch345: glibc-rh1748197-1.patch
Patch346: glibc-rh1748197-2.patch
Patch347: glibc-rh1748197-3.patch
Patch348: glibc-rh1748197-4.patch
Patch349: glibc-rh1748197-5.patch
Patch350: glibc-rh1748197-6.patch
Patch351: glibc-rh1748197-7.patch
Patch352: glibc-rh1642150-4.patch
Patch353: glibc-rh1836867.patch
Patch354: glibc-rh1821531-1.patch
Patch355: glibc-rh1821531-2.patch
Patch356: glibc-rh1845098-1.patch
Patch357: glibc-rh1845098-2.patch
Patch358: glibc-rh1845098-3.patch
Patch359: glibc-rh1871387-1.patch
Patch360: glibc-rh1871387-2.patch
Patch361: glibc-rh1871387-3.patch
Patch362: glibc-rh1871387-4.patch
Patch363: glibc-rh1871387-5.patch
Patch364: glibc-rh1871387-6.patch
Patch365: glibc-rh1871394-1.patch
Patch366: glibc-rh1871394-2.patch
Patch367: glibc-rh1871394-3.patch
Patch368: glibc-rh1871395-1.patch
Patch369: glibc-rh1871395-2.patch
Patch370: glibc-rh1871397-1.patch
Patch371: glibc-rh1871397-2.patch
Patch372: glibc-rh1871397-3.patch
Patch373: glibc-rh1871397-4.patch
Patch374: glibc-rh1871397-5.patch
Patch375: glibc-rh1871397-6.patch
Patch376: glibc-rh1871397-7.patch
Patch377: glibc-rh1871397-8.patch
Patch378: glibc-rh1871397-9.patch
Patch379: glibc-rh1871397-10.patch
Patch380: glibc-rh1871397-11.patch
Patch381: glibc-rh1880670.patch
Patch382: glibc-rh1868106-1.patch
Patch383: glibc-rh1868106-2.patch
Patch384: glibc-rh1868106-3.patch
Patch385: glibc-rh1868106-4.patch
Patch386: glibc-rh1868106-5.patch
Patch387: glibc-rh1868106-6.patch
Patch388: glibc-rh1856398.patch
Patch389: glibc-rh1880670-2.patch
Patch390: glibc-rh1704868-1.patch
Patch391: glibc-rh1704868-2.patch
Patch392: glibc-rh1704868-3.patch
Patch393: glibc-rh1704868-4.patch
Patch394: glibc-rh1704868-5.patch
Patch395: glibc-rh1893662-1.patch
Patch396: glibc-rh1893662-2.patch
Patch397: glibc-rh1855790-1.patch
Patch398: glibc-rh1855790-2.patch
Patch399: glibc-rh1855790-3.patch
Patch400: glibc-rh1855790-4.patch
Patch401: glibc-rh1855790-5.patch
Patch402: glibc-rh1855790-6.patch
Patch403: glibc-rh1855790-7.patch
Patch404: glibc-rh1855790-8.patch
Patch405: glibc-rh1855790-9.patch
Patch406: glibc-rh1855790-10.patch
Patch407: glibc-rh1855790-11.patch
Patch408: glibc-rh1817513-1.patch
Patch409: glibc-rh1817513-2.patch
Patch410: glibc-rh1817513-3.patch
Patch411: glibc-rh1817513-4.patch
Patch412: glibc-rh1817513-5.patch
Patch413: glibc-rh1817513-6.patch
Patch414: glibc-rh1817513-7.patch
Patch415: glibc-rh1817513-8.patch
Patch416: glibc-rh1817513-9.patch
Patch417: glibc-rh1817513-10.patch
Patch418: glibc-rh1817513-11.patch
Patch419: glibc-rh1817513-12.patch
Patch420: glibc-rh1817513-13.patch
Patch421: glibc-rh1817513-14.patch
Patch422: glibc-rh1817513-15.patch
Patch423: glibc-rh1817513-16.patch
Patch424: glibc-rh1817513-17.patch
Patch425: glibc-rh1817513-18.patch
Patch426: glibc-rh1817513-19.patch
Patch427: glibc-rh1817513-20.patch
Patch428: glibc-rh1817513-21.patch
Patch429: glibc-rh1817513-22.patch
Patch430: glibc-rh1817513-23.patch
Patch431: glibc-rh1817513-24.patch
Patch432: glibc-rh1817513-25.patch
Patch433: glibc-rh1817513-26.patch
Patch434: glibc-rh1817513-27.patch
Patch435: glibc-rh1817513-28.patch
Patch436: glibc-rh1817513-29.patch
Patch437: glibc-rh1817513-30.patch
Patch438: glibc-rh1817513-31.patch
Patch439: glibc-rh1817513-32.patch
Patch440: glibc-rh1817513-33.patch
Patch441: glibc-rh1817513-34.patch
Patch442: glibc-rh1817513-35.patch
Patch443: glibc-rh1817513-36.patch
Patch444: glibc-rh1817513-37.patch
Patch445: glibc-rh1817513-38.patch
Patch446: glibc-rh1817513-39.patch
Patch447: glibc-rh1817513-40.patch
Patch448: glibc-rh1817513-41.patch
Patch449: glibc-rh1817513-42.patch
Patch450: glibc-rh1817513-43.patch
Patch451: glibc-rh1817513-44.patch
Patch452: glibc-rh1817513-45.patch
Patch453: glibc-rh1817513-46.patch
Patch454: glibc-rh1817513-47.patch
Patch455: glibc-rh1817513-48.patch
Patch456: glibc-rh1817513-49.patch
Patch457: glibc-rh1817513-50.patch
Patch458: glibc-rh1817513-51.patch
Patch459: glibc-rh1817513-52.patch
Patch460: glibc-rh1817513-53.patch
Patch461: glibc-rh1817513-54.patch
Patch462: glibc-rh1817513-55.patch
Patch463: glibc-rh1817513-56.patch
Patch464: glibc-rh1817513-57.patch
Patch465: glibc-rh1817513-58.patch
Patch466: glibc-rh1817513-59.patch
Patch467: glibc-rh1817513-60.patch
Patch468: glibc-rh1817513-61.patch
Patch469: glibc-rh1817513-62.patch
Patch470: glibc-rh1817513-63.patch
Patch471: glibc-rh1817513-64.patch
Patch472: glibc-rh1817513-65.patch
Patch473: glibc-rh1817513-66.patch
Patch474: glibc-rh1817513-67.patch
Patch475: glibc-rh1817513-68.patch
Patch476: glibc-rh1817513-69.patch
Patch477: glibc-rh1817513-70.patch
Patch478: glibc-rh1817513-71.patch
Patch479: glibc-rh1817513-72.patch
Patch480: glibc-rh1817513-73.patch
Patch481: glibc-rh1817513-74.patch
Patch482: glibc-rh1817513-75.patch
Patch483: glibc-rh1817513-76.patch
Patch484: glibc-rh1817513-77.patch
Patch485: glibc-rh1817513-78.patch
Patch486: glibc-rh1817513-79.patch
Patch487: glibc-rh1817513-80.patch
Patch488: glibc-rh1817513-81.patch
Patch489: glibc-rh1817513-82.patch
Patch490: glibc-rh1817513-83.patch
Patch491: glibc-rh1817513-84.patch
Patch492: glibc-rh1817513-85.patch
Patch493: glibc-rh1817513-86.patch
Patch494: glibc-rh1817513-87.patch
Patch495: glibc-rh1817513-88.patch
Patch496: glibc-rh1817513-89.patch
Patch497: glibc-rh1817513-90.patch
Patch498: glibc-rh1817513-91.patch
Patch499: glibc-rh1817513-92.patch
Patch500: glibc-rh1817513-93.patch
Patch501: glibc-rh1817513-94.patch
Patch502: glibc-rh1817513-95.patch
Patch503: glibc-rh1817513-96.patch
Patch504: glibc-rh1817513-97.patch
Patch505: glibc-rh1817513-98.patch
Patch506: glibc-rh1817513-99.patch
Patch507: glibc-rh1817513-100.patch
Patch508: glibc-rh1817513-101.patch
Patch509: glibc-rh1817513-102.patch
Patch510: glibc-rh1817513-103.patch
Patch511: glibc-rh1817513-104.patch
Patch512: glibc-rh1817513-105.patch
Patch513: glibc-rh1817513-106.patch
Patch514: glibc-rh1817513-107.patch
Patch515: glibc-rh1817513-108.patch
Patch516: glibc-rh1817513-109.patch
Patch517: glibc-rh1817513-110.patch
Patch518: glibc-rh1817513-111.patch
Patch519: glibc-rh1817513-112.patch
Patch520: glibc-rh1817513-113.patch
Patch521: glibc-rh1817513-114.patch
Patch522: glibc-rh1817513-115.patch
Patch523: glibc-rh1817513-116.patch
Patch524: glibc-rh1817513-117.patch
Patch525: glibc-rh1817513-118.patch
Patch526: glibc-rh1817513-119.patch
Patch527: glibc-rh1817513-120.patch
Patch528: glibc-rh1817513-121.patch
Patch529: glibc-rh1817513-122.patch
Patch530: glibc-rh1817513-123.patch
Patch531: glibc-rh1817513-124.patch
Patch532: glibc-rh1817513-125.patch
Patch533: glibc-rh1817513-126.patch
Patch534: glibc-rh1817513-127.patch
Patch535: glibc-rh1817513-128.patch
Patch536: glibc-rh1817513-129.patch
Patch537: glibc-rh1817513-130.patch
Patch538: glibc-rh1817513-131.patch
Patch539: glibc-rh1817513-132.patch
Patch540: glibc-rh1882466-1.patch
Patch541: glibc-rh1882466-2.patch
Patch542: glibc-rh1882466-3.patch
Patch543: glibc-rh1817513-133.patch
Patch544: glibc-rh1912544.patch
Patch545: glibc-rh1918115.patch
Patch546: glibc-rh1924919.patch
Patch547: glibc-rh1932770.patch
Patch548: glibc-rh1936864.patch
Patch549: glibc-rh1871386-1.patch
Patch550: glibc-rh1871386-2.patch
Patch551: glibc-rh1871386-3.patch
Patch552: glibc-rh1871386-4.patch
Patch553: glibc-rh1871386-5.patch
Patch554: glibc-rh1871386-6.patch
Patch555: glibc-rh1871386-7.patch
Patch556: glibc-rh1912670-1.patch
Patch557: glibc-rh1912670-2.patch
Patch558: glibc-rh1912670-3.patch
Patch559: glibc-rh1912670-4.patch
Patch560: glibc-rh1912670-5.patch
Patch561: glibc-rh1930302-1.patch
Patch562: glibc-rh1930302-2.patch
Patch563: glibc-rh1927877.patch
Patch564: glibc-rh1918719-1.patch
Patch565: glibc-rh1918719-2.patch
Patch566: glibc-rh1918719-3.patch
Patch567: glibc-rh1934155-1.patch
Patch568: glibc-rh1934155-2.patch
Patch569: glibc-rh1934155-3.patch
Patch570: glibc-rh1934155-4.patch
Patch571: glibc-rh1934155-5.patch
Patch572: glibc-rh1934155-6.patch
Patch573: glibc-rh1956357-1.patch
Patch574: glibc-rh1956357-2.patch
Patch575: glibc-rh1956357-3.patch
Patch576: glibc-rh1956357-4.patch
Patch577: glibc-rh1956357-5.patch
Patch578: glibc-rh1956357-6.patch
Patch579: glibc-rh1956357-7.patch
Patch580: glibc-rh1956357-8.patch
Patch581: glibc-rh1979127.patch
Patch582: glibc-rh1966472-1.patch
Patch583: glibc-rh1966472-2.patch
Patch584: glibc-rh1966472-3.patch
Patch585: glibc-rh1966472-4.patch
Patch586: glibc-rh1971664-1.patch
Patch587: glibc-rh1971664-2.patch
Patch588: glibc-rh1971664-3.patch
Patch589: glibc-rh1971664-4.patch
Patch590: glibc-rh1971664-5.patch
Patch591: glibc-rh1971664-6.patch
Patch592: glibc-rh1971664-7.patch
Patch593: glibc-rh1971664-8.patch
Patch594: glibc-rh1971664-9.patch
Patch595: glibc-rh1971664-10.patch
Patch596: glibc-rh1971664-11.patch
Patch597: glibc-rh1971664-12.patch
Patch598: glibc-rh1971664-13.patch
Patch599: glibc-rh1971664-14.patch
Patch600: glibc-rh1971664-15.patch
Patch601: glibc-rh1977614.patch
Patch602: glibc-rh1983203-1.patch
Patch603: glibc-rh1983203-2.patch
Patch604: glibc-rh2021452.patch
Patch605: glibc-rh1937515.patch
Patch606: glibc-rh1934162-1.patch
Patch607: glibc-rh1934162-2.patch
Patch608: glibc-rh2000374.patch
Patch609: glibc-rh1991001-1.patch
Patch610: glibc-rh1991001-2.patch
Patch611: glibc-rh1991001-3.patch
Patch612: glibc-rh1991001-4.patch
Patch613: glibc-rh1991001-5.patch
Patch614: glibc-rh1991001-6.patch
Patch615: glibc-rh1991001-7.patch
Patch616: glibc-rh1991001-8.patch
Patch617: glibc-rh1991001-9.patch
Patch618: glibc-rh1991001-10.patch
Patch619: glibc-rh1991001-11.patch
Patch620: glibc-rh1991001-12.patch
Patch621: glibc-rh1991001-13.patch
Patch622: glibc-rh1991001-14.patch
Patch623: glibc-rh1991001-15.patch
Patch624: glibc-rh1991001-16.patch
Patch625: glibc-rh1991001-17.patch
Patch626: glibc-rh1991001-18.patch
Patch627: glibc-rh1991001-19.patch
Patch628: glibc-rh1991001-20.patch
Patch629: glibc-rh1991001-21.patch
Patch630: glibc-rh1991001-22.patch
Patch631: glibc-rh1929928-1.patch
Patch632: glibc-rh1929928-2.patch
Patch633: glibc-rh1929928-3.patch
Patch634: glibc-rh1929928-4.patch
Patch635: glibc-rh1929928-5.patch
Patch636: glibc-rh1984802-1.patch
Patch637: glibc-rh1984802-2.patch
Patch638: glibc-rh1984802-3.patch
Patch639: glibc-rh2023420-1.patch
Patch640: glibc-rh2023420-2.patch
Patch641: glibc-rh2023420-3.patch
Patch642: glibc-rh2023420-4.patch
Patch643: glibc-rh2023420-5.patch
Patch644: glibc-rh2023420-6.patch
Patch645: glibc-rh2023420-7.patch
Patch646: glibc-rh2033648-1.patch
Patch647: glibc-rh2033648-2.patch
Patch648: glibc-rh2036955.patch
Patch649: glibc-rh2033655.patch
Patch650: glibc-rh2007327-1.patch
Patch651: glibc-rh2007327-2.patch
Patch652: glibc-rh2032281-1.patch
Patch653: glibc-rh2032281-2.patch
Patch654: glibc-rh2032281-3.patch
Patch655: glibc-rh2032281-4.patch
Patch656: glibc-rh2032281-5.patch
Patch657: glibc-rh2032281-6.patch
Patch658: glibc-rh2032281-7.patch
Patch659: glibc-rh2045063-1.patch
Patch660: glibc-rh2045063-2.patch
Patch661: glibc-rh2045063-3.patch
Patch662: glibc-rh2045063-4.patch
Patch663: glibc-rh2045063-5.patch
Patch664: glibc-rh2054790.patch
Patch665: glibc-rh2037416-1.patch
Patch666: glibc-rh2037416-2.patch
Patch667: glibc-rh2037416-3.patch
Patch668: glibc-rh2037416-4.patch
Patch669: glibc-rh2037416-5.patch
Patch670: glibc-rh2037416-6.patch
Patch671: glibc-rh2037416-7.patch
Patch672: glibc-rh2037416-8.patch
Patch673: glibc-rh2033684-1.patch
Patch674: glibc-rh2033684-2.patch
Patch675: glibc-rh2033684-3.patch
Patch676: glibc-rh2033684-4.patch
Patch677: glibc-rh2033684-5.patch
Patch678: glibc-rh2033684-6.patch
Patch679: glibc-rh2033684-7.patch
Patch680: glibc-rh2033684-8.patch
Patch681: glibc-rh2033684-9.patch
Patch682: glibc-rh2033684-10.patch
Patch683: glibc-rh2033684-11.patch
Patch684: glibc-rh2033684-12.patch
Patch685: glibc-rh2063712.patch
Patch686: glibc-rh2063042.patch
Patch687: glibc-rh2071745.patch
Patch688: glibc-rh2065588-1.patch
Patch689: glibc-rh2065588-2.patch
Patch690: glibc-rh2065588-3.patch
Patch691: glibc-rh2065588-4.patch
Patch692: glibc-rh2065588-5.patch
Patch693: glibc-rh2065588-6.patch
Patch694: glibc-rh2065588-7.patch
Patch695: glibc-rh2065588-8.patch
Patch696: glibc-rh2065588-9.patch
Patch697: glibc-rh2065588-10.patch
Patch698: glibc-rh2065588-11.patch
Patch699: glibc-rh2065588-12.patch
Patch700: glibc-rh2065588-13.patch
Patch701: glibc-rh2072329.patch
Patch702: glibc-rh1982608.patch
Patch703: glibc-rh1961109.patch
Patch704: glibc-rh2086853.patch
Patch705: glibc-rh2077835.patch
Patch706: glibc-rh2089247-1.patch
Patch707: glibc-rh2089247-2.patch
Patch708: glibc-rh2089247-3.patch
Patch709: glibc-rh2089247-4.patch
Patch710: glibc-rh2089247-5.patch
Patch711: glibc-rh2089247-6.patch
Patch712: glibc-rh2091553.patch
Patch713: glibc-rh1888660.patch
Patch714: glibc-rh2096189-1.patch
Patch715: glibc-rh2096189-2.patch
Patch716: glibc-rh2096189-3.patch
Patch717: glibc-rh2080349-1.patch
Patch718: glibc-rh2080349-2.patch
Patch719: glibc-rh2080349-3.patch
Patch720: glibc-rh2080349-4.patch
Patch721: glibc-rh2080349-5.patch
Patch722: glibc-rh2080349-6.patch
Patch723: glibc-rh2080349-7.patch
Patch724: glibc-rh2080349-8.patch
Patch725: glibc-rh2080349-9.patch
Patch727: glibc-rh2047981-1.patch
Patch728: glibc-rh2047981-2.patch
Patch729: glibc-rh2047981-3.patch
Patch730: glibc-rh2047981-4.patch
Patch731: glibc-rh2047981-5.patch
Patch732: glibc-rh2047981-6.patch
Patch733: glibc-rh2047981-7.patch
Patch734: glibc-rh2047981-8.patch
Patch735: glibc-rh2047981-9.patch
Patch736: glibc-rh2047981-10.patch
Patch737: glibc-rh2047981-11.patch
Patch738: glibc-rh2047981-12.patch
Patch739: glibc-rh2047981-13.patch
Patch740: glibc-rh2047981-14.patch
Patch741: glibc-rh2047981-15.patch
Patch742: glibc-rh2047981-16.patch
Patch743: glibc-rh2047981-17.patch
Patch744: glibc-rh2047981-18.patch
Patch745: glibc-rh2047981-19.patch
Patch746: glibc-rh2047981-20.patch
Patch747: glibc-rh2047981-21.patch
Patch748: glibc-rh2047981-22.patch
Patch749: glibc-rh2047981-23.patch
Patch750: glibc-rh2047981-24.patch
Patch751: glibc-rh2047981-25.patch
Patch752: glibc-rh2047981-26.patch
Patch753: glibc-rh2047981-27.patch
Patch754: glibc-rh2047981-28.patch
Patch755: glibc-rh2047981-29.patch
Patch756: glibc-rh2047981-30.patch
Patch757: glibc-rh2047981-31.patch
Patch758: glibc-rh2047981-32.patch
Patch759: glibc-rh2047981-33.patch
Patch760: glibc-rh2047981-34.patch
Patch761: glibc-rh2047981-35.patch
Patch762: glibc-rh2047981-36.patch
Patch763: glibc-rh2047981-37.patch
Patch764: glibc-rh2047981-38.patch
Patch766: glibc-rh2047981-39.patch
Patch767: glibc-rh2047981-40.patch
Patch768: glibc-rh2047981-41.patch
Patch769: glibc-rh2047981-42.patch
Patch770: glibc-rh2047981-43.patch
Patch771: glibc-rh2047981-44.patch
Patch772: glibc-rh2047981-45.patch
Patch773: glibc-rh2047981-46.patch
Patch774: glibc-rh2047981-47.patch
Patch775: glibc-rh2104907.patch
Patch776: glibc-rh2119304-1.patch
Patch777: glibc-rh2119304-2.patch
Patch778: glibc-rh2119304-3.patch
Patch779: glibc-rh2118667.patch
Patch780: glibc-rh2122498.patch
Patch781: glibc-rh2125222.patch
Patch782: glibc-rh1871383-1.patch
Patch783: glibc-rh1871383-2.patch
Patch784: glibc-rh1871383-3.patch
Patch785: glibc-rh1871383-4.patch
Patch786: glibc-rh1871383-5.patch
Patch787: glibc-rh1871383-6.patch
Patch788: glibc-rh1871383-7.patch
Patch789: glibc-rh2122501-1.patch
Patch790: glibc-rh2122501-2.patch
Patch791: glibc-rh2122501-3.patch
Patch792: glibc-rh2122501-4.patch
Patch793: glibc-rh2122501-5.patch
Patch794: glibc-rh2121746-1.patch
Patch795: glibc-rh2121746-2.patch
Patch796: glibc-rh2116938.patch
Patch797: glibc-rh2109510-1.patch
Patch798: glibc-rh2109510-2.patch
Patch799: glibc-rh2109510-3.patch
Patch800: glibc-rh2109510-4.patch
Patch801: glibc-rh2109510-5.patch
Patch802: glibc-rh2109510-6.patch
Patch803: glibc-rh2109510-7.patch
Patch804: glibc-rh2109510-8.patch
Patch805: glibc-rh2109510-9.patch
Patch806: glibc-rh2109510-10.patch
Patch807: glibc-rh2109510-11.patch
Patch808: glibc-rh2109510-12.patch
Patch809: glibc-rh2109510-13.patch
Patch810: glibc-rh2109510-14.patch
Patch811: glibc-rh2109510-15.patch
Patch812: glibc-rh2109510-16.patch
Patch813: glibc-rh2109510-17.patch
Patch814: glibc-rh2109510-18.patch
Patch815: glibc-rh2109510-19.patch
Patch816: glibc-rh2109510-20.patch
Patch817: glibc-rh2109510-21.patch
Patch818: glibc-rh2109510-22.patch
Patch819: glibc-rh2109510-23.patch
Patch820: glibc-rh2139875-1.patch
Patch821: glibc-rh2139875-2.patch
Patch822: glibc-rh2139875-3.patch
Patch823: glibc-rh1159809-1.patch
Patch824: glibc-rh1159809-2.patch
Patch825: glibc-rh1159809-3.patch
Patch826: glibc-rh1159809-4.patch
Patch827: glibc-rh1159809-5.patch
Patch828: glibc-rh1159809-6.patch
Patch829: glibc-rh1159809-7.patch
Patch830: glibc-rh1159809-8.patch
Patch831: glibc-rh1159809-9.patch
Patch832: glibc-rh1159809-10.patch
Patch833: glibc-rh1159809-11.patch
Patch834: glibc-rh1159809-12.patch
Patch835: glibc-rh2141989.patch
Patch836: glibc-rh2142937-1.patch
Patch837: glibc-rh2142937-2.patch
Patch838: glibc-rh2142937-3.patch
Patch839: glibc-rh2144568.patch
Patch840: glibc-rh2154914-1.patch
Patch841: glibc-rh2154914-2.patch
Patch842: glibc-rh2183081-1.patch
Patch843: glibc-rh2183081-2.patch
Patch844: glibc-rh2172949.patch
Patch845: glibc-rh2180155-1.patch
Patch846: glibc-rh2180155-2.patch
Patch847: glibc-rh2180155-3.patch
Patch848: glibc-rh2213909.patch
Patch849: glibc-rh2176707-1.patch
Patch850: glibc-rh2176707-2.patch
Patch851: glibc-rh2186781.patch
Patch852: glibc-rh2224348.patch
Patch853: glibc-rh2176707-3.patch
Patch854: glibc-rh2180462-1.patch
Patch855: glibc-rh2180462-2.patch
Patch856: glibc-rh2180462-3.patch
Patch857: glibc-rh2180462-4.patch
# (Reverted fixes for rh2233338 were here.)
Patch864: glibc-rh2234714.patch
Patch865: glibc-RHEL-2435.patch
Patch866: glibc-RHEL-2435-2.patch
Patch867: glibc-RHEL-2423.patch
Patch868: glibc-RHEL-3036.patch
Patch869: glibc-RHEL-3757.patch
Patch870: glibc-RHEL-2122.patch
Patch871: glibc-RHEL-1192.patch
Patch872: glibc-RHEL-3639.patch
Patch873: glibc-RHEL-10481.patch
Patch874: glibc-RHEL-13720-1.patch
Patch875: glibc-RHEL-13720-2.patch
Patch876: glibc-RHEL-15867.patch
Patch877: glibc-RHEL-16825-1.patch
Patch878: glibc-RHEL-16825-2.patch
Patch879: glibc-RHEL-16825-3.patch
Patch880: glibc-RHEL-16825-4.patch
Patch881: glibc-RHEL-15696-1.patch
Patch882: glibc-RHEL-15696-2.patch
Patch883: glibc-RHEL-15696-3.patch
Patch884: glibc-RHEL-15696-4.patch
Patch885: glibc-RHEL-15696-5.patch
Patch886: glibc-RHEL-15696-6.patch
Patch887: glibc-RHEL-15696-7.patch
Patch888: glibc-RHEL-15696-8.patch
Patch889: glibc-RHEL-15696-9.patch
Patch890: glibc-RHEL-15696-10.patch
Patch891: glibc-RHEL-15696-11.patch
Patch892: glibc-RHEL-15696-12.patch
Patch893: glibc-RHEL-15696-13.patch
Patch894: glibc-RHEL-15696-14.patch
Patch895: glibc-RHEL-15696-15.patch
Patch896: glibc-RHEL-15696-16.patch
Patch897: glibc-RHEL-15696-17.patch
Patch898: glibc-RHEL-15696-18.patch
Patch899: glibc-RHEL-15696-19.patch
Patch900: glibc-RHEL-15696-20.patch
Patch901: glibc-RHEL-15696-21.patch
Patch902: glibc-RHEL-15696-22.patch
Patch903: glibc-RHEL-15696-23.patch
Patch904: glibc-RHEL-15696-24.patch
Patch905: glibc-RHEL-15696-25.patch
Patch906: glibc-RHEL-15696-26.patch
Patch907: glibc-RHEL-15696-27.patch
Patch908: glibc-RHEL-15696-28.patch
Patch909: glibc-RHEL-15696-29.patch
Patch910: glibc-RHEL-15696-30.patch
Patch911: glibc-RHEL-15696-31.patch
Patch912: glibc-RHEL-15696-32.patch
Patch913: glibc-RHEL-15696-33.patch
Patch914: glibc-RHEL-15696-34.patch
Patch915: glibc-RHEL-15696-35.patch
Patch916: glibc-RHEL-15696-36.patch
Patch917: glibc-RHEL-15696-37.patch
Patch918: glibc-RHEL-15696-38.patch
Patch919: glibc-RHEL-15696-39.patch
Patch920: glibc-RHEL-15696-40.patch
Patch921: glibc-RHEL-15696-41.patch
Patch922: glibc-RHEL-15696-42.patch
Patch923: glibc-RHEL-15696-43.patch
Patch924: glibc-RHEL-15696-44.patch
Patch925: glibc-RHEL-15696-45.patch
Patch926: glibc-RHEL-15696-46.patch
Patch927: glibc-RHEL-15696-47.patch
Patch928: glibc-RHEL-15696-48.patch
Patch929: glibc-RHEL-15696-49.patch
Patch930: glibc-RHEL-15696-50.patch
Patch931: glibc-RHEL-15696-51.patch
Patch932: glibc-RHEL-15696-52.patch
Patch933: glibc-RHEL-15696-53.patch
Patch934: glibc-RHEL-15696-54.patch
Patch935: glibc-RHEL-15696-55.patch
Patch936: glibc-RHEL-15696-56.patch
Patch937: glibc-RHEL-15696-57.patch
Patch938: glibc-RHEL-15696-58.patch
Patch939: glibc-RHEL-15696-59.patch
Patch940: glibc-RHEL-15696-60.patch
Patch941: glibc-RHEL-15696-61.patch
Patch942: glibc-RHEL-15696-62.patch
Patch943: glibc-RHEL-15696-63.patch
Patch944: glibc-RHEL-15696-64.patch
Patch945: glibc-RHEL-15696-65.patch
Patch946: glibc-RHEL-15696-66.patch
Patch947: glibc-RHEL-15696-67.patch
Patch948: glibc-RHEL-15696-68.patch
Patch949: glibc-RHEL-15696-69.patch
Patch950: glibc-RHEL-15696-70.patch
Patch951: glibc-RHEL-15696-71.patch
Patch952: glibc-RHEL-15696-72.patch
Patch953: glibc-RHEL-15696-73.patch
Patch954: glibc-RHEL-15696-74.patch
Patch955: glibc-RHEL-15696-75.patch
Patch956: glibc-RHEL-15696-76.patch
Patch957: glibc-RHEL-15696-77.patch
Patch958: glibc-RHEL-15696-78.patch
Patch959: glibc-RHEL-15696-79.patch
Patch960: glibc-RHEL-15696-80.patch
Patch961: glibc-RHEL-15696-81.patch
Patch962: glibc-RHEL-15696-82.patch
Patch963: glibc-RHEL-15696-83.patch
Patch964: glibc-RHEL-15696-84.patch
Patch965: glibc-RHEL-15696-85.patch
Patch966: glibc-RHEL-15696-86.patch
Patch967: glibc-RHEL-15696-87.patch
Patch968: glibc-RHEL-15696-88.patch
Patch969: glibc-RHEL-15696-89.patch
Patch970: glibc-RHEL-15696-90.patch
Patch971: glibc-RHEL-15696-91.patch
Patch972: glibc-RHEL-15696-92.patch
Patch973: glibc-RHEL-15696-93.patch
Patch974: glibc-RHEL-15696-94.patch
Patch975: glibc-RHEL-15696-95.patch
Patch976: glibc-RHEL-15696-96.patch
Patch977: glibc-RHEL-15696-97.patch
Patch978: glibc-RHEL-15696-98.patch
Patch979: glibc-RHEL-15696-99.patch
Patch980: glibc-RHEL-15696-100.patch
Patch981: glibc-RHEL-15696-101.patch
Patch982: glibc-RHEL-15696-102.patch
Patch983: glibc-RHEL-15696-103.patch
Patch984: glibc-RHEL-15696-104.patch
Patch985: glibc-RHEL-15696-105.patch
Patch986: glibc-RHEL-15696-106.patch
Patch987: glibc-RHEL-15696-107.patch
Patch988: glibc-RHEL-15696-108.patch
Patch989: glibc-RHEL-15696-109.patch
Patch990: glibc-RHEL-15696-110.patch
Patch991: glibc-RHEL-17468-1.patch
Patch992: glibc-RHEL-17468-2.patch
Patch993: glibc-RHEL-19824.patch
Patch994: glibc-RHEL-3010-1.patch
Patch995: glibc-RHEL-3010-2.patch
Patch996: glibc-RHEL-3010-3.patch
Patch997: glibc-RHEL-19445.patch
Patch998: glibc-RHEL-21997.patch
Patch999: glibc-RHEL-31804.patch
Patch1000: glibc-RHEL-34264.patch
Patch1001: glibc-RHEL-34267-1.patch
Patch1002: glibc-RHEL-34267-2.patch
Patch1003: glibc-RHEL-34273.patch
Patch1004: glibc-RHEL-52428-1.patch
Patch1005: glibc-RHEL-52428-2.patch
Patch1006: glibc-RHEL-39994-1.patch
Patch1007: glibc-RHEL-39994-2.patch
Patch1008: glibc-RHEL-36147-1.patch
Patch1009: glibc-RHEL-36147-2.patch
Patch1010: glibc-RHEL-36147-3.patch
Patch1011: glibc-RHEL-49490-1.patch
Patch1012: glibc-RHEL-49490-2.patch
Patch1013: glibc-RHEL-61255.patch
Patch1014: glibc-RHEL-61259-1.patch
Patch1015: glibc-RHEL-61259-2.patch
Patch1016: glibc-RHEL-67806.patch
Patch1017: glibc-RHEL-8381-1.patch
Patch1018: glibc-RHEL-8381-2.patch
Patch1019: glibc-RHEL-8381-3.patch
Patch1020: glibc-RHEL-8381-4.patch
Patch1021: glibc-RHEL-8381-5.patch
Patch1022: glibc-RHEL-8381-6.patch
Patch1023: glibc-RHEL-8381-7.patch
Patch1024: glibc-RHEL-8381-8.patch
Patch1025: glibc-RHEL-8381-9.patch
Patch1026: glibc-RHEL-8381-10.patch
Patch1027: glibc-RHEL-78390.patch
Patch1028: glibc-RHEL-83306-1.patch
Patch1029: glibc-RHEL-83306-2.patch
Patch1030: glibc-RHEL-35280.patch
Patch1031: glibc-RHEL-76211.patch
Patch1032: glibc-RHEL-76387.patch
Patch1033: glibc-RHEL-86018-1.patch
Patch1034: glibc-RHEL-86018-2.patch
Patch1035: glibc-RHEL-88813.patch
Patch1036: glibc-RHEL-71921.patch
Patch1037: glibc-RHEL-92685-1.patch
Patch1038: glibc-RHEL-92685-2.patch
Patch1039: glibc-RHEL-92685-3.patch
Patch1040: glibc-RHEL-92685-4.patch
Patch1041: glibc-RHEL-92685-5.patch
Patch1042: glibc-RHEL-92685-6.patch
Patch1043: glibc-RHEL-92685-7.patch
Patch1044: glibc-RHEL-92685-8.patch
Patch1045: glibc-RHEL-92685-9.patch
Patch1046: glibc-RHEL-93937-1.patch
Patch1047: glibc-RHEL-93937-2.patch
Patch1048: glibc-RHEL-18039-1.patch
Patch1049: glibc-RHEL-18039-2.patch
Patch1050: glibc-RHEL-18039-3.patch
Patch1051: glibc-RHEL-18039-4.patch
Patch1052: glibc-RHEL-18039-5.patch
Patch1053: glibc-RHEL-18039-6.patch
Patch1054: glibc-RHEL-105326.patch
Patch1055: glibc-RHEL-114260.patch
Patch1056: glibc-RHEL-72011-1.patch
Patch1057: glibc-RHEL-72011-2.patch
Patch1058: glibc-RHEL-72011-3.patch
Patch1059: glibc-RHEL-72011-4.patch
Patch1060: glibc-RHEL-72011-5.patch
Patch1061: glibc-RHEL-72011-6.patch
Patch1062: glibc-RHEL-72011-7.patch
Patch1063: glibc-RHEL-72011-8.patch
Patch1064: glibc-RHEL-141849.patch
Patch1065: glibc-RHEL-142194.patch
Patch1066: glibc-RHEL-142787-1.patch
Patch1067: glibc-RHEL-142787-2.patch
Patch1068: glibc-RHEL-24169-1.patch
Patch1069: glibc-RHEL-24169-2.patch
Patch1070: glibc-RHEL-24169-3.patch
Patch1071: glibc-RHEL-24169-4.patch
Patch1072: glibc-RHEL-24169-5.patch
Patch1073: glibc-RHEL-24169-6.patch
Patch1074: glibc-RHEL-24169-7.patch
Patch1075: glibc-RHEL-24169-8.patch
Patch1076: glibc-RHEL-24169-9.patch
Patch1077: glibc-RHEL-24169-10.patch
Patch1078: glibc-RHEL-24169-11.patch
Patch1079: glibc-RHEL-24169-12.patch
Patch1080: glibc-RHEL-24169-13.patch
Patch1081: glibc-RHEL-24169-14.patch
Patch1082: glibc-RHEL-24169-15.patch
Patch1083: glibc-RHEL-24169-16.patch
Patch1084: glibc-RHEL-24169-17.patch
Patch1085: glibc-RHEL-24169-18.patch
Patch1086: glibc-RHEL-24169-19.patch
Patch1087: glibc-RHEL-24169-20.patch
Patch1088: glibc-RHEL-24169-21.patch
Patch1089: glibc-RHEL-24169-22.patch
Patch1090: glibc-RHEL-137185.patch
Patch1091: glibc-RHEL-140104.patch
Patch1092: glibc-RHEL-162891-1.patch
Patch1093: glibc-RHEL-162891-2.patch
Patch1094: glibc-RHEL-162891-3.patch
Patch1095: glibc-RHEL-162891-4.patch
Patch1096: glibc-RHEL-168095.patch
Patch1097: glibc-RHEL-173358-1.patch
Patch1098: glibc-RHEL-173358-2.patch
Patch1099: glibc-RHEL-172700-1.patch
Patch1100: glibc-RHEL-172700-2.patch
Patch1101: glibc-RHEL-172700-3.patch
Patch1102: glibc-RHEL-172700-4.patch
Patch1103: glibc-RHEL-181725.patch
Patch1104: glibc-RHEL-181724-1.patch
Patch1105: glibc-RHEL-181724-2.patch
Patch1106: glibc-RHEL-181724-3.patch
Patch1107: glibc-RHEL-181724-4.patch
Patch1108: glibc-RHEL-181724-5.patch
Patch1109: glibc-RHEL-181724-6.patch
Patch1110: glibc-RHEL-181724-7.patch

##############################################################################
# Continued list of core "glibc" package information:
##############################################################################
Obsoletes: glibc-profile < 2.4
Provides: ldconfig
Provides: /sbin/ldconfig
# Historic file paths provided for backwards compatibility.
Provides: %{glibc_ldso}
%if %{defined glibc_ldso_alternate}
Provides: %{glibc_ldso_alternate}
%endif
Provides: /%{_lib}/libanl.so.1
Provides: /%{_lib}/libc.so.6
Provides: /%{_lib}/libdl.so.2
Provides: /%{_lib}/libm.so.6
Provides: /%{_lib}/libpthread.so.0
Provides: /%{_lib}/libresolv.so.2
Provides: /%{_lib}/librt.so.1
Provides: /%{_lib}/libutil.so.1

# The dynamic linker supports DT_GNU_HASH
Provides: rtld(GNU_HASH)
Requires: glibc-common = %{version}-%{release}

# Various components (regex, glob) have been imported from gnulib.
Provides: bundled(gnulib)

Requires(pre): basesystem

%ifarch %{ix86}
# Automatically install the 32-bit variant if the 64-bit variant has
# been installed.  This covers the case when glibc.i686 is installed
# after nss_db.x86_64.  (See below for the other ordering.)
Recommends: (nss_db(x86-32) if nss_db(x86-64))
%endif

# This is for building auxiliary programs like memusage, nscd
# For initial glibc bootstraps it can be commented out
%if %{without bootstrap}
BuildRequires: gd-devel libpng-devel zlib-devel
%endif
%if %{with docs}
%endif
%if %{without bootstrap}
BuildRequires: libselinux-devel >= 1.33.4-3
%endif
BuildRequires: audit-libs-devel >= 1.1.3, sed >= 3.95, libcap-devel, gettext
# We need procps-ng (/bin/ps), util-linux (/bin/kill), and gawk (/bin/awk),
# but it is more flexible to require the actual programs and let rpm infer
# the packages. However, until bug 1259054 is widely fixed we avoid the
# following:
# BuildRequires: /bin/ps, /bin/kill, /bin/awk
# And use instead (which should be reverted some time in the future):
BuildRequires: procps-ng, util-linux, gawk
BuildRequires: systemtap-sdt-devel

%if %{with valgrind}
# Require valgrind for smoke testing the dynamic loader to make sure we
# have not broken valgrind.
BuildRequires: valgrind
%endif

# We use systemd rpm macros for nscd
BuildRequires: systemd

# We use python for the microbenchmarks and locale data regeneration
# from unicode sources (carried out manually). We choose python3
# explicitly because it supports both use cases.  On some
# distributions, python3 does not actually install /usr/bin/python3,
# so we also depend on python3-devel.
BuildRequires: python3 python3-devel

# This is the first GCC version with -moutline-atomics (#1856398)
BuildRequires: gcc >= 8.3.1-5.2
%define enablekernel 3.2
Conflicts: kernel < %{enablekernel}
%define target %{_target_cpu}-redhat-linux
%ifarch %{arm}
%define target %{_target_cpu}-redhat-linuxeabi
%endif
%ifarch %{power64}
%ifarch ppc64le
%define target ppc64le-redhat-linux
%else
%define target ppc64-redhat-linux
%endif
%endif

# GNU make 4.0 introduced the -O option.
BuildRequires: make >= 4.0

# The intl subsystem generates a parser using bison.
BuildRequires: bison >= 2.7

# binutils 2.30-51 is needed for z13 support on s390x.
BuildRequires: binutils >= 2.30-51

# Earlier releases have broken support for IRELATIVE relocations
Conflicts: prelink < 0.4.2

%if 0%{?_enable_debug_packages}
BuildRequires: elfutils >= 0.72
# -20 adds __find_debuginfo macro
BuildRequires: rpm >= 4.14.3-20
%endif

%if %{without bootstrap}
%if %{with testsuite}
# The testsuite builds static C++ binaries that require a C++ compiler,
# static C++ runtime from libstdc++-static, and lastly static glibc.
BuildRequires: gcc-c++
BuildRequires: libstdc++-static
# A configure check tests for the ability to create static C++ binaries
# before glibc is built and therefore we need a glibc-static for that
# check to pass even if we aren't going to use any of those objects to
# build the tests.
BuildRequires: glibc-static

# libidn2 (but not libidn2-devel) is needed for testing AI_IDN/NI_IDN.
BuildRequires: libidn2
%endif
%endif

# Filter out all GLIBC_PRIVATE symbols since they are internal to
# the package and should not be examined by any other tool.
%global __filter_GLIBC_PRIVATE 1

# For language packs we have glibc require a virtual dependency
# "glibc-langpack" wich gives us at least one installed langpack.
# If no langpack providing 'glibc-langpack' was installed you'd
# get all of them, and that would make the transition from a
# system without langpacks smoother (you'd get all the locales
# installed). You would then trim that list, and the trimmed list
# is preserved. One problem is you can't have "no" locales installed,
# in that case we offer a "glibc-minimal-langpack" sub-pakcage for
# this purpose.
Requires: glibc-langpack = %{version}-%{release}
Suggests: glibc-all-langpacks = %{version}-%{release}

# Suggest extra gconv modules so that they are installed by default but can be
# removed if needed to build a minimal OS image.
Recommends: glibc-gconv-extra%{_isa} = %{version}-%{release}

%description
The glibc package contains standard libraries which are used by
multiple programs on the system. In order to save disk space and
memory, as well as to make upgrading easier, common system code is
kept in one place and shared between programs. This particular package
contains the most important sets of shared libraries: the standard C
library and the standard math library. Without these two libraries, a
Linux system will not function.

######################################################################
# libnsl subpackage
######################################################################

%package -n libnsl
Summary: Legacy support library for NIS
Requires: %{name}%{_isa} = %{version}-%{release}

%description -n libnsl
This package provides the legacy version of libnsl library, for
accessing NIS services.

This library is provided for backwards compatibility only;
applications should use libnsl2 instead to gain IPv6 support.

##############################################################################
# glibc "devel" sub-package
##############################################################################
%package devel
Summary: Object files for development using standard C libraries.
Requires(pre): /sbin/install-info
Requires(pre): %{name}-headers
Requires: %{name}-headers = %{version}-%{release}
Requires: %{name} = %{version}-%{release}
Requires: libgcc%{_isa}
Requires: libxcrypt-devel%{_isa} >= 4.0.0

%description devel
The glibc-devel package contains the object files necessary
for developing programs which use the standard C libraries (which are
used by nearly all programs).  If you are developing programs which
will use the standard C libraries, your system needs to have these
standard object files available in order to create the
executables.

Install glibc-devel if you are going to develop programs which will
use the standard C libraries.

##############################################################################
# glibc "doc" sub-package
##############################################################################
%if %{with docs}
%package doc
Summary: Documentation for GNU libc
BuildArch: noarch
Requires: %{name} = %{version}-%{release}

# Removing texinfo will cause check-safety.sh test to fail because it seems to
# trigger documentation generation based on dependencies.  We need to fix this
# upstream in some way that doesn't depend on generating docs to validate the
# texinfo.  I expect it's simply the wrong dependency for that target.
BuildRequires: texinfo >= 5.0

%description doc
The glibc-doc package contains The GNU C Library Reference Manual in info
format.  Additional package documentation is also provided.
%endif

##############################################################################
# glibc "static" sub-package
##############################################################################
%package static
Summary: C library static libraries for -static linking.
Requires: %{name}-devel = %{version}-%{release}
Requires: libxcrypt-static%{?_isa} >= 4.0.0

%description static
The glibc-static package contains the C library static libraries
for -static linking.  You don't need these, unless you link statically,
which is highly discouraged.

##############################################################################
# glibc "headers" sub-package
# - The headers package includes all common headers that are shared amongst
#   the multilib builds. It was created to reduce the download size, and
#   thus avoid downloading one header package per multilib. The package is
#   identical both in content and file list, any difference is an error.
#   Files like gnu/stubs.h which have gnu/stubs-32.h (i686) and gnu/stubs-64.h
#   are included in glibc-headers, but the -32 and -64 files are in their
#   respective i686 and x86_64 devel packages.
##############################################################################
%package headers
Summary: Header files for development using standard C libraries.
Provides: %{name}-headers(%{_target_cpu})
Requires(pre): kernel-headers
Requires: kernel-headers >= 2.2.1, %{name} = %{version}-%{release}
BuildRequires: kernel-headers >= 3.2

%description headers
The glibc-headers package contains the header files necessary
for developing programs which use the standard C libraries (which are
used by nearly all programs).  If you are developing programs which
will use the standard C libraries, your system needs to have these
standard header files available in order to create the
executables.

Install glibc-headers if you are going to develop programs which will
use the standard C libraries.

##############################################################################
# glibc "common" sub-package
##############################################################################
%package common
Summary: Common binaries and locale data for glibc
Requires: %{name} = %{version}-%{release}
Requires: tzdata >= 2003a

%description common
The glibc-common package includes common binaries for the GNU libc
libraries, as well as national language (locale) support.

######################################################################
# File triggers to do ldconfig calls automatically (see rhbz#1380878)
######################################################################

# File triggers for when libraries are added or removed in standard
# paths.
%transfiletriggerin common -P 2000000 -- /lib /usr/lib /lib64 /usr/lib64
/sbin/ldconfig
%end

%transfiletriggerpostun common -P 2000000 -- /lib /usr/lib /lib64 /usr/lib64
/sbin/ldconfig
%end

# We need to run ldconfig manually because __brp_ldconfig assumes that
# glibc itself is always installed in $RPM_BUILD_ROOT, but with sysroots
# we may be installed into a subdirectory of that path.  Therefore we
# unset __brp_ldconfig and run ldconfig by hand with the sysroots path
# passed to -r.
%undefine __brp_ldconfig

######################################################################

%package locale-source
Summary: The sources for the locales
Requires: %{name} = %{version}-%{release}
Requires: %{name}-common = %{version}-%{release}

%description locale-source
The sources for all locales provided in the language packs.
If you are building custom locales you will most likely use
these sources as the basis for your new locale.

%{lua:
-- Array of languages (ISO-639 codes).
local languages = {}
-- Dictionary from language codes (as in the languages array) to arrays
-- of regions.
local supplements = {}
do
   -- Parse the SUPPORTED file.  Eliminate duplicates.
   local lang_region_seen = {}
   for line in io.lines(rpm.expand("%{SOURCE11}")) do
      -- Match lines which contain a language (eo) or language/region
      -- (en_US) strings.
      local lang_region = string.match(line, "^([a-z][^/@.]+)")
      if lang_region ~= nil then
	 if lang_region_seen[lang_region] == nil then
	    lang_region_seen[lang_region] = true

	    -- Split language/region pair.
	    local lang, region = string.match(lang_region, "^(.+)_(.+)")
	    if lang == nil then
	       -- Region is missing, use only the language.
	       lang = lang_region
	    end
	    local suppl = supplements[lang]
	    if suppl == nil then
	       suppl = {}
	       supplements[lang] = suppl
	       -- New language not seen before.
	       languages[#languages + 1] = lang
	    end
	    if region ~= nil then
	       -- New region because of the check against
	       -- lang_region_seen above.
	       suppl[#suppl + 1] = region
	    end
	 end
      end
   end
   -- Sort for determinism.
   table.sort(languages)
   for _, supples in pairs(supplements) do
      table.sort(supplements)
   end
end

-- Compute the Supplements: list for a language, based on the regions.
local function compute_supplements(lang)
   result = "langpacks-" .. lang
   regions = supplements[lang]
   if regions ~= nil then
      for i = 1, #regions do
	 result = result .. " or langpacks-" .. lang .. "_" .. regions[i]
      end
   end
   return result
end

-- Emit the definition of a language pack package.
local function lang_package(lang)
   local suppl = compute_supplements(lang)
   print(rpm.expand([[

%package langpack-]]..lang..[[

Summary: Locale data for ]]..lang..[[

Provides: glibc-langpack = %{version}-%{release}
Requires: %{name} = %{version}-%{release}
Requires: %{name}-common = %{version}-%{release}
Supplements: (glibc and (]]..suppl..[[))
%description langpack-]]..lang..[[

The glibc-langpack-]]..lang..[[ package includes the basic information required
to support the ]]..lang..[[ language in your applications.
%files -f langpack-]]..lang..[[.filelist langpack-]]..lang..[[
]]))
end

for i = 1, #languages do
   lang_package(languages[i])
end
}

# The glibc-all-langpacks provides the virtual glibc-langpack,
# and thus satisfies glibc's requirement for installed locales.
# Users can add one more other langauge packs and then eventually
# uninstall all-langpacks to save space.
%package all-langpacks
Summary: All language packs for %{name}.
Requires: %{name} = %{version}-%{release}
Requires: %{name}-common = %{version}-%{release}
Provides: %{name}-langpack = %{version}-%{release}
%description all-langpacks

# No %files, this is an empty pacakge. The C/POSIX and
# C.UTF-8 files are already installed by glibc. We create
# minimal-langpack because the virtual provide of
# glibc-langpack needs at least one package installed
# to satisfy it. Given that no-locales installed is a valid
# use case we support it here with this package.
%package minimal-langpack
Summary: Minimal language packs for %{name}.
Provides: glibc-langpack = %{version}-%{release}
Requires: %{name} = %{version}-%{release}
Requires: %{name}-common = %{version}-%{release}
%description minimal-langpack
This is a Meta package that is used to install minimal language packs.
This package ensures you can use C, POSIX, or C.UTF-8 locales, but
nothing else. It is designed for assembling a minimal system.
%files minimal-langpack

# Infrequently used iconv converter modules.
%package gconv-extra
Summary: All iconv converter modules for %{name}.
Requires: %{name}%{_isa} = %{version}-%{release}
Requires: %{name}-common = %{version}-%{release}

%description gconv-extra
This package contains all iconv converter modules built in %{name}.

##############################################################################
# glibc "nscd" sub-package
##############################################################################
%package -n nscd
Summary: A Name Service Caching Daemon (nscd).
Requires: %{name} = %{version}-%{release}
%if %{without bootstrap}
Requires: libselinux >= 1.17.10-1
%endif
Requires: audit-libs >= 1.1.3
Requires(pre): /usr/sbin/useradd, coreutils
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd, /usr/sbin/userdel

%description -n nscd
The nscd daemon caches name service lookups and can improve
performance with LDAP, and may help with DNS as well.

##############################################################################
# Subpackages for NSS modules except nss_files, nss_compat, nss_dns
##############################################################################

# This should remain it's own subpackage or "Provides: nss_db" to allow easy
# migration from old systems that previously had the old nss_db package
# installed. Note that this doesn't make the migration that smooth, the
# databases still need rebuilding because the formats were different.
# The nss_db package was deprecated in F16 and onwards:
# https://lists.fedoraproject.org/pipermail/devel/2011-July/153665.html
# The different database format does cause some issues for users:
# https://lists.fedoraproject.org/pipermail/devel/2011-December/160497.html
%package -n nss_db
Summary: Name Service Switch (NSS) module using hash-indexed files
Requires: %{name}%{_isa} = %{version}-%{release}
%ifarch x86_64
# Automatically install the 32-bit variant if the 64-bit variant has
# been installed.  This covers the case when glibc.i686 is installed
# before nss_db.x86_64.  (See above for the other ordering.)
Recommends: (nss_db(x86-32) if glibc(x86-32))
%endif

%description -n nss_db
The nss_db Name Service Switch module uses hash-indexed files in /var/db
to speed up user, group, service, host name, and other NSS-based lookups.

%package -n nss_hesiod
Summary: Name Service Switch (NSS) module using Hesiod
Requires: %{name}%{_isa} = %{version}-%{release}

%description -n nss_hesiod
The nss_hesiod Name Service Switch module uses the Domain Name System
(DNS) as a source for user, group, and service information, following
the Hesiod convention of Project Athena.

%package nss-devel
Summary: Development files for directly linking NSS service modules
Requires: %{name}%{_isa} = %{version}-%{release}
Requires: nss_db%{_isa} = %{version}-%{release}
Requires: nss_hesiod%{_isa} = %{version}-%{release}

%description nss-devel
The glibc-nss-devel package contains the object files necessary to
compile applications and libraries which directly link against NSS
modules supplied by glibc.

This is a rare and special use case; regular development has to use
the glibc-devel package instead.

##############################################################################
# glibc "utils" sub-package
##############################################################################
%package utils
Summary: Development utilities from GNU C library
Requires: %{name} = %{version}-%{release}

%description utils
The glibc-utils package contains memusage, a memory usage profiler,
mtrace, a memory leak tracer and xtrace, a function call tracer
which can be helpful during program debugging.

If unsure if you need this, don't install this package.

%if %{with benchtests}
%package benchtests
Summary: Benchmarking binaries and scripts for %{name}
%description benchtests
This package provides built benchmark binaries and scripts to run
microbenchmark tests on the system.
%endif

##############################################################################
# compat-libpthread-nonshared
# See: https://sourceware.org/bugzilla/show_bug.cgi?id=23500
##############################################################################
%package -n compat-libpthread-nonshared
Summary: Compatibility support for linking against libpthread_nonshared.a.

%description -n compat-libpthread-nonshared
This package provides compatibility support for applications that expect
libpthread_nonshared.a to exist. The support provided is in the form of
an empty libpthread_nonshared.a that allows dynamic links to succeed.
Such applications should be adjusted to avoid linking against
libpthread_nonshared.a which is no longer used. The static library
libpthread_nonshared.a is an internal implementation detail of the C
runtime and should not be expected to exist.

##############################################################################
# Prepare for the build.
##############################################################################
%prep
%autosetup -n %{glibcsrcdir} -p1

##############################################################################
# %%prep - Additional prep required...
##############################################################################
# Make benchmark scripts executable
chmod +x benchtests/scripts/*.py scripts/pylint

# Remove all files generated from patching.
find . -type f -size 0 -o -name "*.orig" -exec rm -f {} \;

# Ensure timestamps on configure files are current to prevent
# regenerating them.
touch `find . -name configure`

# Ensure *-kw.h files are current to prevent regenerating them.
touch locale/programs/*-kw.h

# Verify that our copy of localedata/SUPPORTED matches the glibc
# version.
#
# The separate file copy is used by the Lua parser above.
# Patches or new upstream versions may change the list of locales,
# which changes the set of langpacks we need to build.  Verify the
# differences then update the copy of SUPPORTED.  This approach has
# two purposes: (a) avoid spurious changes to the set of langpacks,
# and (b) the Lua snippet can use a fully patched-up version
# of the localedata/SUPPORTED file.
diff -u %{SOURCE11} localedata/SUPPORTED

##############################################################################
# Build glibc...
##############################################################################
%build
# Log system information
uname -a
LD_SHOW_AUXV=1 /bin/true
cat /proc/cpuinfo
cat /proc/sysinfo 2>/dev/null || true
cat /proc/meminfo
df

# We build using the native system compilers.
GCC=gcc
GXX=g++

# Part of rpm_inherit_flags.  Is overridden below.
rpm_append_flag ()
{
    BuildFlags="$BuildFlags $*"
}

# Propagates the listed flags to rpm_append_flag if supplied by
# redhat-rpm-config.
BuildFlags="-O2 -g"
rpm_inherit_flags ()
{
	local reference=" $* "
	local flag
	for flag in $RPM_OPT_FLAGS $RPM_LD_FLAGS ; do
		if echo "$reference" | grep -q -F " $flag " ; then
			rpm_append_flag "$flag"
		fi
	done
}

# Propgate select compiler flags from redhat-rpm-config.  These flags
# are target-dependent, so we use only those which are specified in
# redhat-rpm-config.  We keep the -m32/-m32/-m64 flags to support
# multilib builds.
#
# Note: For building alternative run-times, care is required to avoid
# overriding the architecture flags which go into CC/CXX.  The flags
# below are passed in CFLAGS.

rpm_inherit_flags \
	"-Wp,-D_GLIBCXX_ASSERTIONS" \
	"-fasynchronous-unwind-tables" \
	"-fstack-clash-protection" \
	"-funwind-tables" \
	"-m31" \
	"-m32" \
	"-m64" \
	"-march=i686" \
	"-march=x86-64" \
	"-march=z13" \
	"-march=z14" \
	"-march=zEC12" \
	"-mfpmath=sse" \
	"-msse2" \
	"-mstackrealign" \
	"-mtune=generic" \
	"-mtune=z13" \
	"-mtune=z14" \
	"-mtune=zEC12" \
	"-specs=/usr/lib/rpm/redhat/redhat-annobin-cc1" \

# Propagate additional build flags to BuildFlagsNonshared.  This is
# very special because some of these files are part of the startup
# code.  We essentially hope that these flags have little effect
# there, and only specify the, for consistency, so that annobin
# records the expected compiler flags.
BuildFlagsNonshared=
rpm_append_flag () {
    BuildFlagsNonshared="$BuildFlagsNonshared $*"
}
rpm_inherit_flags \
	"-Wp,-D_FORTIFY_SOURCE=2" \

# Special flag to enable annobin annotations for statically linked
# assembler code.  Needs to be passed to make; not preserved by
# configure.
%define glibc_make_flags_as ASFLAGS="-g -Wa,--generate-missing-build-notes=yes"
%define glibc_make_flags %{glibc_make_flags_as}

%ifarch aarch64
# BZ 1856398 - Build AArch64 with out-of-line support for LSE atomics
GCC="$GCC -moutline-atomics"
GXX="$GXX -moutline-atomics"
%endif

##############################################################################
# %%build - Generic options.
##############################################################################
EnableKernel="--enable-kernel=%{enablekernel}"
# Save the used compiler and options into the file "Gcc" for use later
# by %%install.
echo "$GCC" > Gcc

##############################################################################
# build()
#	Build glibc in `build-%{target}$1', passing the rest of the arguments
#	as CFLAGS to the build (not the same as configure CFLAGS). Several
#	global values are used to determine build flags, kernel version,
#	system tap support, etc.
##############################################################################
build()
{
	local builddir=build-%{target}${1:+-$1}
	${1+shift}
	rm -rf $builddir
	mkdir $builddir
	pushd $builddir
	../configure CC="$GCC" CXX="$GXX" CFLAGS="$BuildFlags $*" \
		--prefix=%{_prefix} \
		--with-headers=%{_prefix}/include $EnableKernel \
		--with-nonshared-cflags="$BuildFlagsNonshared" \
		--enable-bind-now \
		--build=%{target} \
		--enable-stack-protector=strong \
%ifarch %{pie_arches}
		--enable-static-pie \
%endif
		--enable-tunables \
		--enable-systemtap \
		${core_with_options} \
%ifarch x86_64 %{ix86}
	       --enable-cet \
%endif
%ifarch %{ix86}
		--disable-multi-arch \
%endif
%if %{without werror}
		--disable-werror \
%endif
		--disable-profile \
%if %{with bootstrap}
		--without-selinux \
%endif
		--disable-crypt ||
		{ cat config.log; false; }

	make %{?_smp_mflags} -O -r %{glibc_make_flags}
	popd
}

# Default set of compiler options.
build

%if %{buildpower9}
(
  GCC="$GCC -mcpu=power9 -mtune=power9"
  GXX="$GXX -mcpu=power9 -mtune=power9"
  core_with_options="--with-cpu=power9"
  build power9
)
%endif

##############################################################################
# Install glibc...
##############################################################################
%install

# The built glibc is installed into a subdirectory of $RPM_BUILD_ROOT.
# For a system glibc that subdirectory is "/" (the root of the filesystem).
# This is called a sysroot (system root) and can be changed if we have a
# distribution that supports multiple installed glibc versions.
%define glibc_sysroot $RPM_BUILD_ROOT

# Ensure the permissions of errlist.c do not change.  When the file is
# regenerated the Makefile sets the permissions to 444. We set it to 644
# to match what comes out of git. The tarball of the git archive won't have
# correct permissions because git doesn't track all of the permissions
# accurately (see git-cache-meta if you need that). We also set it to 644 to
# match pre-existing rpms. We do this *after* the build because the build
# might regenerate the file and set the permissions to 444.
chmod 644 sysdeps/gnu/errlist.c

# Reload compiler and build options that were used during %%build.
GCC=`cat Gcc`

# Create symbolic links for UsrMove.
# Do not include sbin to work around lorax bug RHEL-67332.
# See below: Remove UsrMove symbolic links.
usrmove_file_names="bin lib lib64"
for d in $usrmove_file_names ; do
    mkdir -p "%{glibc_sysroot}/usr/$d"
    ln -s "usr/$d" "%{glibc_sysroot}/$d"
done

%ifarch riscv64
# RISC-V ABI wants to install everything in /usr/lib64/lp64d.
# Make these be symlinks to /usr/lib64.  See:
# Make these be symlinks to /lib64 or /usr/lib64 respectively.  See:
# https://lists.fedoraproject.org/archives/list/devel@lists.fedoraproject.org/thread/DRHT5YTPK4WWVGL3GIN5BF2IKX2ODHZ3/
for d in %{glibc_sysroot}%{_libdir}; do
	mkdir -p $d
	(cd $d && ln -sf . lp64d)
done
%endif

# Build and install:
make -j1 install_root=%{glibc_sysroot} install -C build-%{target}

# Install all of the supported locales.
pushd build-%{target}
# Do not use a parallel make here because the hardlink optimization in
# localedef is not fully reproducible when running concurrently.
make install_root=%{glibc_sysroot} \
	install-locales -C ../localedata objdir=`pwd`
popd

# install_different:
#	Install all core libraries into DESTDIR/SUBDIR. Either the file is
#	installed as a copy or a symlink to the default install (if it is the
#	same). The path SUBDIR_UP is the prefix used to go from
#	DESTDIR/SUBDIR to the default installed libraries e.g.
#	ln -s SUBDIR_UP/foo.so DESTDIR/SUBDIR/foo.so.
#	When you call this function it is expected that you are in the root
#	of the build directory, and that the default build directory is:
#	"../build-%{target}" (relatively).
#	The primary use of this function is to install alternate runtimes
#	into the build directory and avoid duplicating this code for each
#	runtime.
install_different()
{
	local lib libbase libbaseso dlib
	local destdir="$1"
	local subdir="$2"
	local subdir_up="$3"
	local libdestdir="$destdir/$subdir"
	# All three arguments must be non-zero paths.
	if ! [ "$destdir" \
	       -a "$subdir" \
	       -a "$subdir_up" ]; then
		echo "One of the arguments to install_different was emtpy."
		exit 1
	fi
	# Create the destination directory and the multilib directory.
	mkdir -p "$destdir"
	mkdir -p "$libdestdir"
	# Walk all of the libraries we installed...
	for lib in libc math/libm nptl/libpthread rt/librt nptl_db/libthread_db
	do
		libbase=${lib#*/}
		# Take care that `libbaseso' has a * that needs expanding so
		# take care with quoting.
		libbaseso=$(basename %{glibc_sysroot}%{_libdir}/${libbase}-*.so)
		# Only install if different from default build library.
		if cmp -s ${lib}.so ../build-%{target}/${lib}.so; then
			ln -sf "$subdir_up"/$libbaseso $libdestdir/$libbaseso
		else
			cp -a ${lib}.so $libdestdir/$libbaseso
		fi
		dlib=$libdestdir/$(basename %{glibc_sysroot}%{_libdir}/${libbase}.so.*)
		ln -sf $libbaseso $dlib
	done
}

%if %{buildpower9}
pushd build-%{target}-power9
install_different "$RPM_BUILD_ROOT/%{_lib}/glibc-hwcaps" power9 "../.."
popd
%endif

##############################################################################
# Remove the files we don't want to distribute
##############################################################################

# Remove the libNoVersion files.
# XXX: This looks like a bug in glibc that accidentally installed these
#      wrong files. We probably don't need this today.
rm -f %{glibc_sysroot}/%{_libdir}/libNoVersion*

# Remove the old nss modules.
rm -f %{glibc_sysroot}%{_libdir}/libnss1-*
rm -f %{glibc_sysroot}%{_libdir}/libnss-*.so.1

# This statically linked binary is no longer necessary in a world where
# the default Fedora install uses an initramfs, and further we have rpm-ostree
# which captures the whole userspace FS tree.
# Further, see https://github.com/projectatomic/rpm-ostree/pull/1173#issuecomment-355014583
rm -f %{glibc_sysroot}/{usr/,}sbin/sln

######################################################################
# Run ldconfig to create all the symbolic links we need
######################################################################

# Note: This has to happen before creating /etc/ld.so.conf.

mkdir -p %{glibc_sysroot}/var/cache/ldconfig
truncate -s 0 %{glibc_sysroot}/var/cache/ldconfig/aux-cache

# ldconfig is statically linked, so we can use the new version.
%{glibc_sysroot}/sbin/ldconfig -N -r %{glibc_sysroot}

##############################################################################
# Install info files
##############################################################################

%if %{with docs}
# Move the info files if glibc installed them into the wrong location.
if [ -d %{glibc_sysroot}%{_prefix}/info -a "%{_infodir}" != "%{_prefix}/info" ]; then
  mkdir -p %{glibc_sysroot}%{_infodir}
  mv -f %{glibc_sysroot}%{_prefix}/info/* %{glibc_sysroot}%{_infodir}
  rm -rf %{glibc_sysroot}%{_prefix}/info
fi

# Compress all of the info files.
gzip -9nvf %{glibc_sysroot}%{_infodir}/libc*

# Copy the debugger interface documentation over to the right location
mkdir -p %{glibc_sysroot}%{_docdir}/glibc
cp elf/rtld-debugger-interface.txt %{glibc_sysroot}%{_docdir}/glibc
cp posix/gai.conf %{glibc_sysroot}%{_docdir}/glibc
%else
rm -f %{glibc_sysroot}%{_infodir}/dir
rm -f %{glibc_sysroot}%{_infodir}/libc.info*
%endif

##############################################################################
# Create locale sub-package file lists
##############################################################################

olddir=`pwd`
pushd %{glibc_sysroot}%{_prefix}/lib/locale
rm -f locale-archive
# Intentionally we do not pass --alias-file=, aliases will be added
# by build-locale-archive.
$olddir/build-%{target}/testrun.sh \
        $olddir/build-%{target}/locale/localedef \
        --prefix %{glibc_sysroot} --add-to-archive \
        eo *_*
# Setup the locale-archive template for use by glibc-all-langpacks.  We
# copy the archive in place to keep the size of the file. Even though we
# mark the file with "ghost" the size is used by rpm to compute the
# required free space (see rhbz#1725131). We do this because there is a
# point in the install when build-locale-archive has copied 100% of the
# template into the new locale archive and so this consumes twice the
# amount of diskspace. Note that this doesn't account for copying
# existing compiled locales into the archive, this may consume even more
# disk space and we can't fix that issue. In upstream we have moved away
# from this process, removing build-locale-archive and installing a
# default locale-archive without modification, and leaving compiled
# locales as they are (without inclusion into the archive).
cp locale-archive{,.tmpl}

# Almost half the LC_CTYPE files in langpacks are identical to the C.utf8
# variant which is installed by default.  When we keep them as hardlinks,
# each langpack ends up retaining a copy.  If we convert these to symbolic
# links instead, we save ~350K each when they get installed that way.
#
# LC_MEASUREMENT and LC_PAPER also have several duplicates but we don't
# bother with these because they are only ~30 bytes each.
pushd %{glibc_sysroot}/usr/lib/locale
for f in $(find eo *_* -samefile C.utf8/LC_CTYPE); do
  rm $f && ln -s '../C.utf8/LC_CTYPE' $f
done
popd

# Create the file lists for the language specific sub-packages:
for i in eo *_*
do
    lang=${i%%_*}
    if [ ! -e langpack-${lang}.filelist ]; then
        echo "%dir %{_prefix}/lib/locale" >> langpack-${lang}.filelist
    fi
    echo "%dir  %{_prefix}/lib/locale/$i" >> langpack-${lang}.filelist
    echo "%{_prefix}/lib/locale/$i/*" >> langpack-${lang}.filelist
done
popd
pushd %{glibc_sysroot}%{_prefix}/share/locale
for i in */LC_MESSAGES/libc.mo
do
    locale=${i%%%%/*}
    lang=${locale%%%%_*}
    echo "%lang($lang) %{_prefix}/share/locale/${i}" \
         >> %{glibc_sysroot}%{_prefix}/lib/locale/langpack-${lang}.filelist
done
popd
mv  %{glibc_sysroot}%{_prefix}/lib/locale/*.filelist .

##############################################################################
# Install configuration files for services
##############################################################################

install -p -m 644 nss/nsswitch.conf %{glibc_sysroot}/etc/nsswitch.conf

# This is for ncsd - in glibc 2.2
install -m 644 nscd/nscd.conf %{glibc_sysroot}/etc
mkdir -p %{glibc_sysroot}%{_tmpfilesdir}
install -m 644 %{SOURCE4} %{buildroot}%{_tmpfilesdir}
mkdir -p %{glibc_sysroot}%{_prefix}/lib/systemd/system
install -m 644 nscd/nscd.service nscd/nscd.socket %{glibc_sysroot}%{_prefix}/lib/systemd/system

# Include ld.so.conf
echo 'include ld.so.conf.d/*.conf' > %{glibc_sysroot}/etc/ld.so.conf
truncate -s 0 %{glibc_sysroot}/etc/ld.so.cache
chmod 644 %{glibc_sysroot}/etc/ld.so.conf
mkdir -p %{glibc_sysroot}/etc/ld.so.conf.d
mkdir -p %{glibc_sysroot}/etc/sysconfig
truncate -s 0 %{glibc_sysroot}/etc/sysconfig/nscd
truncate -s 0 %{glibc_sysroot}/etc/gai.conf

# Include %{_libdir}/gconv/gconv-modules.cache
truncate -s 0 %{glibc_sysroot}%{_libdir}/gconv/gconv-modules.cache
chmod 644 %{glibc_sysroot}%{_libdir}/gconv/gconv-modules.cache

##############################################################################
# Install debug copies of unstripped static libraries
# - This step must be last in order to capture any additional static
#   archives we might have added.
##############################################################################

# Remove any zoneinfo files; they are maintained by tzdata.
rm -rf %{glibc_sysroot}%{_prefix}/share/zoneinfo

# Make sure %config files have the same timestamp across multilib packages.
#
# XXX: Ideally ld.so.conf should have the timestamp of the spec file, but there
# doesn't seem to be any macro to give us that.  So we do the next best thing,
# which is to at least keep the timestamp consistent. The choice of using
# SOURCE0 is arbitrary.
touch -r %{SOURCE0} %{glibc_sysroot}/etc/ld.so.conf
touch -r sunrpc/etc.rpc %{glibc_sysroot}/etc/rpc

pushd build-%{target}
$GCC -Os -g \
%ifarch %{pie_arches}
	-fPIE \
	-static-pie \
%else
	-static \
%endif
	 -o build-locale-archive %{SOURCE1} \
	../build-%{target}/locale/locarchive.o \
	../build-%{target}/locale/md5.o \
	../build-%{target}/locale/record-status.o \
	-I. -DDATADIR=\"%{_datadir}\" -DPREFIX=\"%{_prefix}\" \
	-L../build-%{target} \
	-B../build-%{target}/csu/ -lc -lc_nonshared
install -m 700 build-locale-archive %{glibc_sysroot}%{_prefix}/sbin/build-locale-archive
popd

%ifarch s390x
# Compatibility symlink
mkdir -p %{glibc_sysroot}/lib
ln -sf %{_libdir}/ld64.so.1 %{glibc_sysroot}/lib/ld64.so.1
%endif

%if %{with benchtests}
# Build benchmark binaries.  Ignore the output of the benchmark runs.
pushd build-%{target}
make BENCH_DURATION=1 bench-build
popd

# Copy over benchmark binaries.
mkdir -p %{glibc_sysroot}%{_prefix}/libexec/glibc-benchtests
cp $(find build-%{target}/benchtests -type f -executable) %{glibc_sysroot}%{_prefix}/libexec/glibc-benchtests/
# ... and the makefile.
for b in %{SOURCE9} %{SOURCE10}; do
	cp $b %{glibc_sysroot}%{_prefix}/libexec/glibc-benchtests/
done
# .. and finally, the comparison scripts.
cp benchtests/scripts/benchout.schema.json %{glibc_sysroot}%{_prefix}/libexec/glibc-benchtests/
cp benchtests/scripts/compare_bench.py %{glibc_sysroot}%{_prefix}/libexec/glibc-benchtests/
cp benchtests/scripts/import_bench.py %{glibc_sysroot}%{_prefix}/libexec/glibc-benchtests/
cp benchtests/scripts/validate_benchout.py %{glibc_sysroot}%{_prefix}/libexec/glibc-benchtests/
%endif

%if 0%{?_enable_debug_packages}
# The #line directives gperf generates do not give the proper
# file name relative to the build directory.
pushd locale
ln -s programs/*.gperf .
popd
pushd iconv
ln -s ../locale/programs/charmap-kw.gperf .
popd
%endif

%if %{with docs}
# Remove the `dir' info-heirarchy file which will be maintained
# by the system as it adds info files to the install.
rm -f %{glibc_sysroot}%{_infodir}/dir
%endif

mkdir -p %{glibc_sysroot}/var/{db,run}/nscd
touch %{glibc_sysroot}/var/{db,run}/nscd/{passwd,group,hosts,services}
touch %{glibc_sysroot}/var/run/nscd/{socket,nscd.pid}

# Strip all of the installed object files.
strip -g %{glibc_sysroot}%{_libdir}/*.o

###############################################################################
# Rebuild libpthread.a using --whole-archive to ensure all of libpthread
# is included in a static link. This prevents any problems when linking
# statically, using parts of libpthread, and other necessary parts not
# being included. Upstream has decided that this is the wrong approach to
# this problem and that the full set of dependencies should be resolved
# such that static linking works and produces the most minimally sized
# static application possible.
###############################################################################
pushd %{glibc_sysroot}%{_libdir}/
$GCC -r -nostdlib -o libpthread.o -Wl,--whole-archive ./libpthread.a
rm libpthread.a
ar rcs libpthread.a libpthread.o
rm libpthread.o
popd

# The xtrace and memusage scripts have hard-coded paths that need to be
# translated to a correct set of paths using the $LIB token which is
# dynamically translated by ld.so as the default lib directory.
for i in %{glibc_sysroot}%{_prefix}/bin/{xtrace,memusage}; do
%if %{with bootstrap}
  test -w $i || continue
%endif
  sed -e 's~=/%{_lib}/libpcprofile.so~=%{_libdir}/libpcprofile.so~' \
      -e 's~=/%{_lib}/libmemusage.so~=%{_libdir}/libmemusage.so~' \
      -e 's~='\''/\\\$LIB/libpcprofile.so~='\''%{_prefix}/\\$LIB/libpcprofile.so~' \
      -e 's~='\''/\\\$LIB/libmemusage.so~='\''%{_prefix}/\\$LIB/libmemusage.so~' \
      -i $i
done

##############################################################################
# Build an empty libpthread_nonshared.a for compatiliby with applications
# that have old linker scripts that reference this file. We ship this only
# in compat-libpthread-nonshared sub-package.
##############################################################################
ar cr %{glibc_sysroot}%{_libdir}/libpthread_nonshared.a

# Remove UsrMove symbolic links.
# These should not end in the packaged contents.
# They are part of the filesystem package.
for d in $usrmove_file_names ; do
    rm "%{glibc_sysroot}/$d"
done

##############################################################################
# Beyond this point in the install process we no longer modify the set of
# installed files.
##############################################################################

# Placement of files in subpackages is mostly controlled by the
# %%files section below.  There are some exceptions where a subset of
# files are put in one package and need to be elided from another
# package, and it's not possible to do this easily using explicit file
# lists or directory matching.  For these exceptions. .filelist file
# are created.

# Make the sorting below more consistent.
export LC_ALL=C

# `make_sysroot_filelist PATH FIND-ARGS LIST` writes %%files section
# lines for files and directories in the sysroot under PATH to the
# file LIST, with FIND-ARGS passed to the find command.  The output is
# passed through sort.
make_sysroot_filelist () {
  (
    find "%{glibc_sysroot}$1" \( -type f -o -type l \) $2 \
      -printf "$1/%%P\n" || true
    find "%{glibc_sysroot}$1" -type d $2 -printf "%%%%dir $1/%%P\n" || true
  ) | sort > "$3"
}

# `remove_from_filelist FILE1 FILE2` removes the lines from FILE1
# which are also in FILE2.  The lines must not contain tabs, and the
# file is sorted as a side effect.  The input files must be sorted
# according to the sort command.
remove_from_filelist () {
    comm -23 "$1" "$2" > "$1.tmp"
    mv "$1.tmp" "$1"
}

# `split_sysroot_file_list DIR FIND-ARGS REGEXP MAIN-LIST EXCEPTIONS-LIST`
# creates a list of files in the sysroot subdirectory # DIR.
# Files and directories are enumerated with the find command,
# passing FIND-ARGS as an extra argument.  Those output paths that
# match REGEXP (an POSIX extended regular expression; all whitespace
# in it is removed before matching) are put into EXCEPTIONS-LIST.  The
# remaining files are put into MAIN-LIST.
split_sysroot_file_list () {
  make_sysroot_filelist "$1" "$2" "$4"
  grep -E -e "$(printf %%s "$3" | tr -d '[:space:]')" < "$4" > "$5"
  remove_from_filelist "$4" "$5"
}

# glibc-devel historically contains a subset of the files in
# /usr/include/gnu.  The remaining headers are in glibc-headers.
# The -regex clause skips /usr/include, which is owned by the
# filesystem package.
split_sysroot_file_list \
  %{_includedir} '( ! -regex .*%{_includedir}$ )' \
  '%{_includedir}/gnu/(stubs|lib-names)-.*\.h$' \
  headers.filelist devel.filelist

# The primary gconv converters are in the glibc package, the rest goes
# into glibc-gconv-extra.  The Z9 and Z900 subpatterns are for
# s390x-specific converters.  The -name clause skips over files
# that are not loadable gconv modules.
split_sysroot_file_list \
  %{_libdir}/gconv '-name *.so' \
  'gconv/
   (ANSI_X3\.110
   |CP1252
   |ISO8859-15?
   |UNICODE
   |UTF-[0-9]+
   |ISO-8859-1_CP037_Z900
   |UTF(8|16)_UTF(16|32)_Z9
   )\.so$' \
  gconv-extra.filelist glibc.filelist

##############################################################################
# Run the glibc testsuite
##############################################################################
%check
%if %{with testsuite}

# Run the glibc tests. If any tests fail to build we exit %check with
# an error, otherwise we print the test failure list and the failed
# test output and continue.  Write to standard error to avoid
# synchronization issues with make and shell tracing output if
# standard output and standard error are different pipes.
run_tests () {
  # This hides a test suite build failure, which should be fatal.  We
  # check "Summary of test results:" below to verify that all tests
  # were built and run.
  make %{?_smp_mflags} -O check |& tee rpmbuild.check.log >&2
  test -n tests.sum
  if ! grep -q '^Summary of test results:$' rpmbuild.check.log ; then
    echo "FAIL: test suite build of target: $(basename "$(pwd)")" >& 2
    exit 1
  fi
  set +x
  grep -v ^PASS: tests.sum > rpmbuild.tests.sum.not-passing || true
  if test -n rpmbuild.tests.sum.not-passing ; then
    echo ===================FAILED TESTS===================== >&2
    echo "Target: $(basename "$(pwd)")" >& 2
    cat rpmbuild.tests.sum.not-passing >&2
    while read failed_code failed_test ; do
      for suffix in out test-result ; do
        if test -e "$failed_test.$suffix"; then
	  echo >&2
          echo "=====$failed_code $failed_test.$suffix=====" >&2
          cat -- "$failed_test.$suffix" >&2
	  echo >&2
        fi
      done
    done <rpmbuild.tests.sum.not-passing
  fi

  # Unconditonally dump differences in the system call list.
  echo "* System call consistency checks:" >&2
  cat misc/tst-syscall-list.out >&2
  set -x
}

# Increase timeouts
export TIMEOUTFACTOR=16
parent=$$
echo ====================TESTING=========================

# Default libraries.
pushd build-%{target}
run_tests
popd

%if %{buildpower9}
echo ====================TESTING -mcpu=power9=============
pushd build-%{target}-power9
run_tests
popd
%endif



echo ====================TESTING END=====================
PLTCMD='/^Relocation section .*\(\.rela\?\.plt\|\.rela\.IA_64\.pltoff\)/,/^$/p'
echo ====================PLT RELOCS LD.SO================
readelf -Wr %{glibc_sysroot}%{_libdir}/ld-*.so | sed -n -e "$PLTCMD"
echo ====================PLT RELOCS LIBC.SO==============
readelf -Wr %{glibc_sysroot}%{_libdir}/libc-*.so | sed -n -e "$PLTCMD"
echo ====================PLT RELOCS END==================

# Obtain a way to run the dynamic loader.  Avoid matching the symbolic
# link and then pick the first loader (although there should be only
# one).
run_ldso="$(find %{glibc_sysroot}%{_libdir}/ld-*.so -type f | LC_ALL=C sort | head -n1) --library-path %{glibc_sysroot}%{_libdir}"

# Show the auxiliary vector as seen by the new library
# (even if we do not perform the valgrind test).
LD_SHOW_AUXV=1 $run_ldso /bin/true

# Finally, check if valgrind runs with the new glibc.
# We want to fail building if valgrind is not able to run with this glibc so
# that we can then coordinate with valgrind to get it fixed before we update
# glibc.
pushd build-%{target}

# Show the auxiliary vector as seen by the new library
# (even if we do not perform the valgrind test).
LD_SHOW_AUXV=1 $run_ldso /bin/true

%if %{with valgrind}
$run_ldso /usr/bin/valgrind --error-exitcode=1 \
	$run_ldso /usr/bin/true
%endif
popd

%comment Matches: %if %{with testsuite}
%endif


%pre -p <lua>
-- Check that the running kernel is new enough
required = '%{enablekernel}'
rel = posix.uname("%r")
if rpm.vercmp(rel, required) < 0 then
  error("FATAL: kernel too old", 0)
end

%post -p <lua>
%glibc_post_funcs
-- (1) Remove multilib libraries from previous installs.
-- In order to support in-place upgrades, we must immediately remove
-- obsolete platform directories after installing a new glibc
-- version.  RPM only deletes files removed by updates near the end
-- of the transaction.  If we did not remove the obsolete platform
-- directories here, they may be preferred by the dynamic linker
-- during the execution of subsequent RPM scriptlets, likely
-- resulting in process startup failures.

-- Full set of libraries glibc may install.
install_libs = { "anl", "BrokenLocale", "c", "dl", "m", "mvec",
		 "nss_compat", "nss_db", "nss_dns", "nss_files",
		 "nss_hesiod", "pthread", "resolv", "rt", "SegFault",
		 "thread_db", "util" }

-- We are going to remove these libraries. Generally speaking we remove
-- all core libraries in the multilib directory.
-- We employ a tight match where X.Y is in [2.0,9.9*], so we would
-- match "libc-2.0.so" and so on up to "libc-9.9*".
remove_regexps = {}
for i = 1, #install_libs do
  remove_regexps[i] = ("lib" .. install_libs[i]
                       .. "%%-[2-9]%%.[0-9]+%%.so$")
end

-- Two exceptions:
remove_regexps[#install_libs + 1] = "libthread_db%%-1%%.0%%.so"
remove_regexps[#install_libs + 2] = "libSegFault%%.so"

-- We are going to search these directories.
local remove_dirs = { "%{_libdir}/i686",
		      "%{_libdir}/i686/nosegneg",
		      "%{_libdir}/power6",
		      "%{_libdir}/power7",
		      "%{_libdir}/power8",
		      "%{_libdir}/power9"}

-- Walk all the directories with files we need to remove...
for _, rdir in ipairs (remove_dirs) do
  if posix.access (rdir) then
    -- If the directory exists we look at all the files...
    local remove_files = posix.files (rdir)
    for rfile in remove_files do
      for _, rregexp in ipairs (remove_regexps) do
	-- Does it match the regexp?
	local dso = string.match (rfile, rregexp)
        if (dso ~= nil) then
	  -- Removing file...
	  os.remove (rdir .. '/' .. rfile)
	end
      end
    end
  end
end

-- (2) Update /etc/ld.so.conf
-- Next we update /etc/ld.so.conf to ensure that it starts with
-- a literal "include ld.so.conf.d/*.conf".

local ldsoconf = "/etc/ld.so.conf"
local ldsoconf_tmp = "/etc/glibc_post_upgrade.ld.so.conf"

if posix.access (ldsoconf) then

  -- We must have a "include ld.so.conf.d/*.conf" line.
  local have_include = false
  for line in io.lines (ldsoconf) do
    -- This must match, and we don't ignore whitespace.
    if string.match (line, "^include ld.so.conf.d/%%*%%.conf$") ~= nil then
      have_include = true
    end
  end

  if not have_include then
    -- Insert "include ld.so.conf.d/*.conf" line at the start of the
    -- file. We only support one of these post upgrades running at
    -- a time (temporary file name is fixed).
    local tmp_fd = io.open (ldsoconf_tmp, "w")
    if tmp_fd ~= nil then
      tmp_fd:write ("include ld.so.conf.d/*.conf\n")
      for line in io.lines (ldsoconf) do
        tmp_fd:write (line .. "\n")
      end
      tmp_fd:close ()
      local res = os.rename (ldsoconf_tmp, ldsoconf)
      if res == nil then
        io.stdout:write ("Error: Unable to update configuration file (rename).\n")
      end
    else
      io.stdout:write ("Error: Unable to update configuration file (open).\n")
    end
  end
end

-- (3) Rebuild ld.so.cache early.
-- If the format of the cache changes then we need to rebuild
-- the cache early to avoid any problems running binaries with
-- the new glibc.

-- Note: We use _prefix because Fedora's UsrMove says so.
post_exec ("%{_prefix}/sbin/ldconfig")

-- (4) Update gconv modules cache.
-- If the /usr/lib/gconv/gconv-modules.cache exists, then update it
-- with the latest set of modules that were just installed.
-- We assume that the cache is in _libdir/gconv and called
-- "gconv-modules.cache".

update_gconv_modules_cache()

%posttrans all-langpacks -e -p <lua>
-- If at the end of the transaction we are still installed
-- (have a template of non-zero size), then we rebuild the
-- locale cache (locale-archive) from the pre-populated
-- locale cache (locale-archive.tmpl) i.e. template.
if posix.stat("%{_prefix}/lib/locale/locale-archive.tmpl", "size") > 0 then
  pid = posix.fork()
  if pid == 0 then
    posix.exec("%{_prefix}/sbin/build-locale-archive", "--install-langs", "%%{_install_langs}")
  elseif pid > 0 then
    posix.wait(pid)
  end
end

%postun all-langpacks -p <lua>
-- In the postun we remove the locale cache if unstalling.
-- (build-locale-archive will delete the archive during an upgrade.)
if arg[2] == 0 then
  os.remove("%{_prefix}/lib/locale/locale-archive")
end

%if %{with docs}
%post devel
/sbin/install-info %{_infodir}/libc.info.gz %{_infodir}/dir > /dev/null 2>&1 || :
%endif

%pre headers
# this used to be a link and it is causing nightmares now
if [ -L %{_prefix}/include/scsi ] ; then
  rm -f %{_prefix}/include/scsi
fi

%if %{with docs}
%preun devel
if [ "$1" = 0 ]; then
  /sbin/install-info --delete %{_infodir}/libc.info.gz %{_infodir}/dir > /dev/null 2>&1 || :
fi
%endif

%post gconv-extra -p <lua>
%glibc_post_funcs
update_gconv_modules_cache ()

%postun gconv-extra -p <lua>
%glibc_post_funcs
update_gconv_modules_cache ()

%pre -n nscd
getent group nscd >/dev/null || /usr/sbin/groupadd -g 28 -r nscd
getent passwd nscd >/dev/null ||
  /usr/sbin/useradd -M -o -r -d / -s /sbin/nologin \
		    -c "NSCD Daemon" -u 28 -g nscd nscd

%post -n nscd
%systemd_post nscd.service

%preun -n nscd
%systemd_preun nscd.service

%postun -n nscd
if test $1 = 0; then
  /usr/sbin/userdel nscd > /dev/null 2>&1 || :
fi
%systemd_postun_with_restart nscd.service

%files -f glibc.filelist
/sbin/ldconfig
%{_sbindir}/iconvconfig
%{_libexecdir}/getconf
%{_prefix}%{glibc_ldso}
%{?glibc_ldso_alternate}
%{_libdir}/ld-%{version}.so
%{_libdir}/libBrokenLocale-%{version}.so
%{_libdir}/libBrokenLocale.so.1
%{_libdir}/libSegFault.so
%{_libdir}/libanl-%{version}.so
%{_libdir}/libanl.so.1
%{_libdir}/libc-%{version}.so
%{_libdir}/libc.so.6
%{_libdir}/libdl-%{version}.so
%{_libdir}/libdl.so.2
%{_libdir}/libm-%{version}.so
%{_libdir}/libm.so.6
%{_libdir}/libnss_compat-%{version}.so
%{_libdir}/libnss_compat.so.2
%{_libdir}/libnss_dns-%{version}.so
%{_libdir}/libnss_dns.so.2
%{_libdir}/libnss_files-%{version}.so
%{_libdir}/libnss_files.so.2
%{_libdir}/libpthread-%{version}.so
%{_libdir}/libpthread.so.0
%{_libdir}/libresolv-%{version}.so
%{_libdir}/libresolv.so.2
%{_libdir}/librt-%{version}.so
%{_libdir}/librt.so.1
%{_libdir}/libthread_db-1.0.so
%{_libdir}/libthread_db.so.1
%{_libdir}/libutil-%{version}.so
%{_libdir}/libutil.so.1
%{_libdir}/libmemusage.so
%{_libdir}/libpcprofile.so
%{_libdir}/audit
%if %{glibc_has_libmvec}
%{_libdir}/libmvec-%{version}.so
%{_libdir}/libmvec.so.1
%endif
%if %{buildpower9}
%{_libdir}/glibc-hwcaps
%endif
%verify(not md5 size mtime link) %config(noreplace) /etc/nsswitch.conf
%verify(not md5 size mtime) %config(noreplace) /etc/ld.so.conf
%verify(not md5 size mtime) %config(noreplace) /etc/rpc
%dir /etc/ld.so.conf.d
%dir %{_libdir}/gconv
%dir %{_libdir}/gconv/gconv-modules.d
%verify(not md5 size mtime) %config(noreplace) %{_libdir}/gconv/gconv-modules
%verify(not md5 size mtime) %{_libdir}/gconv/gconv-modules.cache
%ifarch s390x
%verify(not md5 size mtime) %config(noreplace) %{_libdir}/gconv/gconv-modules.d/gconv-modules-s390.conf
%endif
%dir %attr(0700,root,root) /var/cache/ldconfig
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/cache/ldconfig/aux-cache
%attr(0644,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /etc/ld.so.cache
%attr(0644,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /etc/gai.conf
# If rpm doesn't support %license, then use %doc instead.
%{!?_licensedir:%global license %%doc}
%license COPYING COPYING.LIB LICENSES

%files common
%{_bindir}/catchsegv
%{_bindir}/gencat
%{_bindir}/getconf
%{_bindir}/getent
%{_bindir}/iconv
%{_bindir}/ld.so
%{_bindir}/ldd
%{_bindir}/locale
%{_bindir}/localedef
%{_bindir}/makedb
%{_bindir}/pldd
%{_bindir}/sotruss
%{_bindir}/sprof
%{_bindir}/tzselect
%{_sbindir}/zdump
%{_sbindir}/zic
%dir %{_datarootdir}/i18n
%dir %{_datarootdir}/i18n/locales
%dir %{_datarootdir}/i18n/charmaps
%dir %{_prefix}/lib/locale
%{_datarootdir}/locale/locale.alias
%{_prefix}/lib/locale/C.utf8
%ifarch %{ix86}
%{_bindir}/lddlibc4
%endif

%files all-langpacks
%attr(0644,root,root) %verify(not md5 size mtime) %{_prefix}/lib/locale/locale-archive.tmpl
%attr(0644,root,root) %verify(not md5 size mtime mode) %ghost %{_prefix}/lib/locale/locale-archive
# build-locale-archive re-generates locale-archive during install/upgrade/downgrade
%attr(0700,root,root) %{_prefix}/sbin/build-locale-archive

%files locale-source
%{_datarootdir}/i18n/locales
%{_datarootdir}/i18n/charmaps

%files -f devel.filelist devel
%{_libdir}/*.o
%{_libdir}/libBrokenLocale.so
%{_libdir}/libanl.so
%{_libdir}/libc.so
%{_libdir}/libc_nonshared.a
%{_libdir}/libdl.so
%{_libdir}/libg.a
%{_libdir}/libm.so
%{_libdir}/libmcheck.a
%{_libdir}/libpthread.so
%{_libdir}/libpthread_nonshared.a
%{_libdir}/libresolv.so
%{_libdir}/librt.so
%{_libdir}/libthread_db.so
%{_libdir}/libutil.so
%if %{glibc_has_libnldbl}
%{_libdir}/libnldbl_nonshared.a
%endif
%if %{glibc_has_libmvec}
%{_libdir}/libmvec.so
%{_libdir}/libmvec_nonshared.a
%endif

%if %{with docs}
%files doc
%{_datarootdir}/doc
%{_infodir}/*.info*
%endif

%files static
%{_libdir}/libBrokenLocale.a
%{_libdir}/libanl.a
%{_libdir}/libc.a
%{_libdir}/libdl.a
%{_libdir}/libm.a
%{_libdir}/libpthread.a
%{_libdir}/libresolv.a
%{_libdir}/librt.a
%{_libdir}/libutil.a
%if %{glibc_has_libmvec}
%{_libdir}/libm-%{version}.a
%{_libdir}/libmvec.a
%endif

%files -f headers.filelist headers

%files utils
%{_bindir}/memusage
%{_bindir}/memusagestat
%{_bindir}/mtrace
%{_bindir}/pcprofiledump
%{_bindir}/xtrace

%files -f gconv-extra.filelist gconv-extra
%verify(not md5 size mtime) %config(noreplace) %{_libdir}/gconv/gconv-modules.d/gconv-modules-extra.conf

%files -n nscd
%{_sbindir}/nscd
%config(noreplace) /etc/nscd.conf
%dir %attr(0755,root,root) /var/run/nscd
%dir %attr(0755,root,root) /var/db/nscd
%{_prefix}/lib/systemd/system/nscd.service
%{_prefix}/lib/systemd/system/nscd.socket
%{_tmpfilesdir}/nscd.conf
%attr(0644,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/run/nscd/nscd.pid
%attr(0666,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/run/nscd/socket
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/run/nscd/passwd
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/run/nscd/group
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/run/nscd/hosts
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/run/nscd/services
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/db/nscd/passwd
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/db/nscd/group
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/db/nscd/hosts
%attr(0600,root,root) %verify(not md5 size mtime) %ghost %config(missingok,noreplace) /var/db/nscd/services
%ghost %config(missingok,noreplace) /etc/sysconfig/nscd

%files -n nss_db
%{_libdir}/libnss_db-%{version}.so
%{_libdir}/libnss_db.so.2
/var/db/Makefile
%files -n nss_hesiod
%{_libdir}/libnss_hesiod-%{version}.so
%{_libdir}/libnss_hesiod.so.2
%doc hesiod/README.hesiod
%files nss-devel
%{_libdir}/libnss_compat.so
%{_libdir}/libnss_db.so
%{_libdir}/libnss_dns.so
%{_libdir}/libnss_files.so
%{_libdir}/libnss_hesiod.so

%files -n libnsl
%{_libdir}/libnsl-%{version}.so
%{_libdir}/libnsl.so.1

%if %{with benchtests}
%files benchtests
%{_libexecdir}/glibc-benchtests
%endif

%files -n compat-libpthread-nonshared
%{_libdir}/libpthread_nonshared.a

%changelog
* Tue Jul 07 2026 DJ Delorie <dj@redhat.com> - 2.28-251.40
- CVE-2026-5435 Out-of-bounds write via TSIG record processing (RHEL-181724)

* Thu Jun 25 2026 Patsy Griffin <patsy@redhat.com> - 2.28-251.39
- CVE-2026-5928: Fix ungetwc operating on byte stream (RHEL-181725)

* Tue Jun 02 2026 Patsy Griffin <patsy@redhat.com> - 2.28-251.38
- CVE-2026-5450: Fix buffer overflow in scanf (RHEL-172700)

* Mon May 11 2026 Arjun Shankar <arjun@redhat.com> - 2.28-251.37
- Add tests for CVE-2026-4437 and CVE-2026-4438 (RHEL-173358)

* Mon May 11 2026 Frédéric Bérat <fberat@redhat.com> - 2.28-251.36
- Fix __nss_get_default_domain logic to restore netgroup user enumeration
  (RHEL-168095)

* Mon May 04 2026 Frédéric Bérat <fberat@redhat.com> - 2.28-251.35
- CVE-2026-4046: Fix assertion failure in IBM1390 and IBM1399 iconv modules
  (RHEL-162891)

* Wed Apr 15 2026 Patsy Griffin <patsy@redhat.com> - 2.28-251.34
- Update locales for Croatia to use EUR as currency symbol. (RHEL-140104)

* Mon Apr 13 2026 Patsy Griffin <patsy@redhat.com> - 2.28-251.33
- Change the currency symbol for the bg_BG locale to euro. (RHEL-137185)

* Wed Apr 01 2026 Patsy Griffin <patsy@redhat.com> - 2.28-251.32
- Improve qsort implementation (RHEL-24169)

* Mon Jan 26 2026 Florian Weimer  <fweimer@redhat.com> - 2.28-251.31
- CVE-2025-15281: wordexp WRDE_REUSE uninitialized memory read (RHEL-142787)

* Mon Jan 19 2026 Florian Weimer  <fweimer@redhat.com> - 2.28-251.30
- Remove default /var/tmp for LD_PROFILE_OUTPUT (RHEL-142194)

* Mon Jan 19 2026 Florian Weimer  <fweimer@redhat.com> - 2.28-251.29
- rpminspect.yaml: note that glibc-minimal-langpack is empty (RHEL-123889)

* Fri Jan 16 2026 Florian Weimer  <fweimer@redhat.com> - 2.28-251.28
- CVE-2026-0915: Stack memory disclosure in getnetbyaddr (RHEL-141849)

* Thu Nov 20 2025 Arjun Shankar <arjun@redhat.com> - 2.28-251.27
- Fix a segmentation fault in multi-threaded multi-namespace programs using
  ctype.h macros (RHEL-72011)

* Fri Oct 03 2025 DJ Delorie <dj@redhat.com> - 2.28-251.26
- nss: Group merge does not react to ERANGE during merge (RHEL-114260)

* Thu Jul 24 2025 Florian Weimer  <fweimer@redhat.com> - 2.28-251.25
- CVE-2025-8058: Double free in regcomp (RHEL-105326)

* Thu Jun 05 2025 Patsy Griffin <patsy@redhat.com> - 2.28-251.24
- Keep reloading /etc/resolv.conf after timeouts with getaddrinfo
  and AF_UNSPEC.
- Avoid timeouts with getaddrinfo, AF_UNSPEC, and certain DNS error
  responses. (RHEL-18039)

* Thu Jun 05 2025 Arjun Shankar <arjun@redhat.com> - 2.28-251.23
- Reduce spurious rebuilds while running tests (RHEL-93937)

* Mon May 26 2025 Florian Weimer  <fweimer@redhat.com> - 2.28-251.22
- CVE-2025-4802: static setuid dlopen may search LD_LIBRARY_PATH (RHEL-92685)

* Wed May 14 2025 Patsy Griffin <patsy@redhat.com> - 2.28-251.21
- elf: Keep using minimal malloc after early DTV resize (RHEL-71921)

* Fri May 02 2025 Patsy Griffin <patsy@redhat.com> - 2.28-251.20
- Add missing libnss_testX.so requirement for tst-nss-test3 (RHEL-88813)

* Fri Apr 25 2025 Patsy Griffin <patsy@redhat.com> - 2.28-251.19
- libio: Fix a deadlock after fork in popen
- libio: Correctly link tst-popen-fork against libpthread (RHEL-86018)

* Fri Apr 18 2025 Patsy Griffin <patsy@redhat.com> - 2.28-251.18
- x86: Avoid integer truncation with large cache sizes (RHEL-76387)

* Wed Apr 16 2025 Patsy Griffin <patsy@redhat.com> - 2.28-251.17
- x86: Check the lower byte of EAX of CPUID leaf 2 (RHEL-76211)

* Thu Mar 27 2025 Arjun Shankar <arjun@redhat.com> - 2.28-251.16
- nscd: Fix an unlikely TTL issue in the netgroup cache (RHEL-35280)

* Thu Mar 20 2025 Arjun Shankar <arjun@redhat.com> - 2.28-251.15
- CVE-2025-0395: Fix a buffer overflow in assert (RHEL-83306)

* Tue Feb 11 2025 Patsy Griffin <patsy@redhat.com> - 2.28-251.14
- Correct locking and cancellation cleanup in syslog functions (RHEL-78390)

* Fri Feb  7 2025 Carlos O'Donell <carlos@redhat.com> - 2.28-251.13
- Restore internal ABI to avoid tooling false positives (RHEL-8381)

* Thu Jan 30 2025 Carlos O'Donell <carlos@redhat.com> - 2.28-251.12
- Fix missed wakeup in POSIX thread condition variables (RHEL-8381)

* Mon Dec  9 2024 DJ Delorie <dj@redhat.com> - 2.28-251.11
- add GB18030-2022 charmap and tests (RHEL-67806)

* Thu Nov 21 2024 Patsy Griffin <patsy@redhat.com> - 2.28-251.10
- Remove some unused ppc64le string functions (RHEL-61259)

* Wed Nov 13 2024 Florian Weimer <fweimer@redhat.com> - 2.28-251.9
- Use /sbin/ldconfig path for lorax compatibility (RHEL-63048)

* Mon Nov 11 2024 Patsy Griffin <patsy@redhat.com> - 2.28-251.8
- aarch64: MTE compatible strncmp (RHEL-61255)

* Wed Oct 23 2024 Florian Weimer <fweimer@redhat.com> - 2.28-251.7
- Use UsrMove path destination in the RPM files (RHEL-63048)

* Tue Sep 17 2024 Patsy Griffin <patsy@redhat.com> - 2.28-251.6
- s390x: Fix segfault in wcsncmp
- Enhanced test coverage for strncmp, wcsncmp (RHEL-49490)

* Fri Aug 16 2024 Patsy Griffin <patsy@redhat.com> - 2.28-251.5
- elf: Clarify and invert second argument of _dl_allocate_tls_init
- elf: Avoid re-initializing already allocated TLS in dlopen (RHEL-36147)

* Thu Aug  8 2024 Patsy Griffin <patsy@redhat.com> - 2.28-251.4
- elf: Avoid some free (NULL) calls in _dl_update_slotinfo
- elf: Support recursive use of dynamic TLS in interposed malloc (RHEL-39994)

* Mon Aug  5 2024 Patsy Griffin <patsy@redhat.com> - 2.28-251.3
- Update i386 libm-test-ulps (RHEL-52428)

* Fri Apr 26 2024 Florian Weimer <fweimer@redhat.com> - 2.28-251.2
- CVE-2024-33599: nscd: buffer overflow in netgroup cache (RHEL-34264)
- CVE-2024-33600: nscd: null pointer dereferences in netgroup cache (RHEL-34267)
- CVE-2024-33601: nscd: crash on out-of-memory condition (RHEL-34271)
- CVE-2024-33602: nscd: memory corruption with NSS netgroup modules (RHEL-34273)

* Mon Apr 15 2024 Florian Weimer <fweimer@redhat.com> - 2.28-251.1
- CVE-2024-2961: Out of bounds write in iconv conversion to ISO-2022-CN-EXT (RHEL-31804)

* Thu Jan 18 2024 Florian Weimer <fweimer@redhat.com> - 2.28-251
- Cache information in x86_64 ld.so --list-diagnostics output (RHEL-21997)

* Wed Jan 10 2024 Arjun Shankar <arjun@redhat.com> - 2.28-250
- getaddrinfo: Return correct error EAI_MEMORY when out-of-memory (RHEL-19445)

* Wed Jan  3 2024 Florian Weimer <fweimer@redhat.com> - 2.28-249
- Updates for AMD cache size computation (RHEL-3010)

* Tue Jan  2 2024 Florian Weimer <fweimer@redhat.com> - 2.28-248
- Re-enable output buffering for wide stdio streams (RHEL-19824)

* Thu Dec 21 2023 Carlos O'Donell <carlos@redhat.com> - 2.28-247
- Fix TLS corruption during dlopen()/dlclose() sequences (RHEL-17468)

* Thu Dec 14 2023 DJ Delorie <dj@redhat.com> - 2.28-246
- Include CentOS Hyperscaler SIG patches backported by Intel (RHEL-15696)

* Fri Dec  8 2023 Florian Weimer <fweimer@redhat.com> - 2.28-245
- Improve compatibility between underlinking and IFUNC resolvers (RHEL-16825)

* Fri Nov 24 2023 Florian Weimer <fweimer@redhat.com> - 2.28-244
- Restore <sys/cdefs.h> compatibility with C90 compilers (RHEL-15867)

* Tue Nov 21 2023 Florian Weimer <fweimer@redhat.com> - 2.28-243
- ldconfig should skip temporary files created by RPM (RHEL-13720)

* Mon Nov 20 2023 Florian Weimer <fweimer@redhat.com> - 2.28-242
- Fix force-first handling in dlclose (RHEL-10481)

* Fri Nov 10 2023 Florian Weimer <fweimer@redhat.com> - 2.28-241
- Avoid lazy binding failures during dlclose (RHEL-3639)

* Tue Oct 24 2023 Arjun Shankar <arjun@redhat.com> - 2.28-240
- Add /usr/share/doc/glibc/gai.conf to glibc-doc (RHEL-12894)

* Fri Oct 20 2023 Florian Weimer <fweimer@redhat.com> - 2.28-239
- nscd: Skip unusable entries in first pass in prune_cache (RHEL-1192)

* Mon Oct 16 2023 DJ Delorie <dj@redhat.com> - 2.28-238
- Fix slow tls access after dlopen (RHEL-2122)

* Mon Oct 16 2023 Arjun Shankar <arjun@redhat.com> - 2.28-237
- Enable running a single test from the testsuite (RHEL-3757)

* Wed Sep 20 2023 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-236.7
- CVE-2023-4911 glibc: buffer overflow in ld.so leading to privilege escalation (RHEL-3036)

* Tue Sep 19 2023 Carlos O'Donell <carlos@redhat.com> - 2.28-236.6
- Revert: Always call destructors in reverse constructor order (#2233338)

* Tue Sep 19 2023 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-236.5
- CVE-2023-4806 glibc: potential use-after-free in getaddrinfo (RHEL-2423)

* Tue Sep 19 2023 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-236.4
- CVE-2023-4813: Work around RHEL-8 limitation in test (RHEL-2435)

* Fri Sep 15 2023 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-236.3
- CVE-2023-4813: potential use-after-free in gaih_inet (RHEL-2435)

* Wed Sep 13 2023 Florian Weimer <fweimer@redhat.com> - 2.28-236.2
- CVE-2023-4527: Stack read overflow in getaddrinfo in no-aaaa mode (#2234714)

* Mon Sep 11 2023 Florian Weimer <fweimer@redhat.com> - 2.28-236.1
- Always call destructors in reverse constructor order (#2233338)

* Tue Aug 15 2023 Carlos O'Donell <carlos@redhat.com> - 2.28-236
- Fix string and memory function tuning on small systems (#2180462)

* Tue Aug  8 2023 DJ Delorie <dj@redhat.com> - 2.28-235
- Fix temporal threshold calculations (#2180462)

* Mon Aug  7 2023 Florian Weimer <fweimer@redhat.com> - 2.28-234
- Ignore symbolic link change on /etc/nsswitch.conf (#2229709)

* Wed Jul 26 2023 DJ Delorie <dj@redhat.com> - 2.28-233
- Update test to closer match upstream. (#2176707)

* Fri Jul 21 2023 Florian Weimer <fweimer@redhat.com> - 2.28-232
- Make libSegFault.so NODELETE (#2224348)

* Sun Jul  9 2023 Carlos O'Donell <carlos@redhat.com> - 2.28-231
- Update ESTALE error message translations (#2186781)

* Fri Jul 7 2023 DJ Delorie <dj@redhat.com> - 2.28-230
- Don't block SIGCHILD when system() is called concurrently (#2176707)

* Mon Jul 3 2023 DJ Delorie <dj@redhat.com> - 2.28-229
- resolv_conf: release lock on allocation failure (#2213909)

* Mon May 22 2023 Florian Weimer <fweimer@redhat.com> - 2.28-228
- gmon: Various bug fixes (#2180155)

* Thu May 18 2023 Patsy Griffin <patsy@redhat.com> - 2.28-227
- Change sgetsgent_r to set errno. (#2172949)

* Wed May  3 2023 Florian Weimer <fweimer@redhat.com> - 2.28-226
- Fix incorrect inline feraiseexcept on i686, x86-64 (#2183081)

* Fri Jan 20 2023 Florian Weimer <fweimer@redhat.com> - 2.28-225
- Enforce a specififc internal ordering for tunables (#2154914)

* Wed Nov 30 2022 Arjun Shankar <arjun@redhat.com> - 2.28-224
- Fix rtld-audit trampoline for aarch64 (#2144568)

* Fri Nov 25 2022 Arjun Shankar <arjun@redhat.com> - 2.28-223
- Backport upstream fixes to tst-pldd (#2142937)

* Tue Nov 22 2022 Florian Weimer <fweimer@redhat.com> - 2.28-222
- Restore IPC_64 support in sysvipc *ctl functions (#2141989)

* Fri Nov 18 2022 Florian Weimer <fweimer@redhat.com> - 2.28-221
- Switch to fast DSO dependency sorting algorithm (#1159809)

* Thu Nov  3 2022 Florian Weimer <fweimer@redhat.com> - 2.28-220
- Explicitly switch to --with-default-link=no (#2109510)
- Define MAP_SYNC on ppc64le (#2139875)

* Mon Oct 24 2022 Arjun Shankar <arjun@redhat.com> - 2.28-219
- Fix -Wstrict-overflow warning when using CMSG_NXTHDR macro (#2116938)

* Fri Oct 14 2022 DJ Delorie <dj@redhat.com> - 2.28-218
- Fix dlmopen/dlclose/dlmopen sequence and libc initialization (#2121746)

* Thu Oct 13 2022 Arjun Shankar <arjun@redhat.com> - 2.28-217
- Fix memory corruption in printf with thousands separators and large
  integer width (#2122501)

* Wed Oct 05 2022 Arjun Shankar <arjun@redhat.com> - 2.28-216
- Retain .gnu_debuglink section for libc.so.6 (#2115830)
- Remove .annobin* symbols from ld.so
- Remove redundant ld.so debuginfo file

* Wed Sep 28 2022 DJ Delorie <dj@redhat.com> - 2.28-215
- Improve malloc implementation (#1871383)

* Tue Sep 20 2022 Florian Weimer <fweimer@redhat.com> - 2.28-214
- Fix hwcaps search path size computation (#2125222)

* Tue Sep 20 2022 Florian Weimer <fweimer@redhat.com> - 2.28-213
- Fix nscd netlink cache invalidation if epoll is used (#2122498)

* Tue Sep 20 2022 Florian Weimer <fweimer@redhat.com> - 2.28-212
- Run tst-audit-tlsdesc, tst-audit-tlsdesc-dlopen everywhere (#2118667)

* Thu Aug 25 2022 Florian Weimer <fweimer@redhat.com> - 2.28-211
- Preserve GLRO (dl_naudit) internal ABI (#2119304)
- Avoid s390x ABI change due to z16 recognition on s390x (#2119304)

* Tue Aug 23 2022 Arjun Shankar <arjun@redhat.com> - 2.28-210
- Fix locale en_US@ampm (#2104907)

* Fri Jul 22 2022 Carlos O'Donell <carlos@redhat.com> - 2.28-209
- Improve dynamic loader auditing interface (LD_AUDIT) (#2047981)
- Add dlinfo() API support for RTLD_DI_PHDR (#2097898)

* Fri Jul 15 2022 Patsy Griffin <patsy@redhat.com> - 2.28-208
- Update syscall-names.list to Linux 5.18. (#2080349)

* Fri Jun 24 2022 Florian Weimer <fweimer@redhat.com> - 2.28-207
- Add the no-aaaa DNS stub resolver option (#2096189)

* Thu Jun  9 2022 Arjun Shankar <arjun@redhat.com> - 2.28-206
- Fix deadlocks in pthread_atfork handlers (#1888660)

* Tue Jun 07 2022 DJ Delorie <dj@redhat.com) - 2.28-205
- Fix incorrect strncpy results on POWER9 (#2091553)

* Mon May 23 2022 Florian Weimer <fweimer@redhat.com> - 2.28-204
- Increase tempnam randomness (#2089247)

* Tue May 17 2022 Patsy Griffin <patsy@redhat.com> - 2.28-203
- 390x: Add support for IBM z16. (#2077835)

* Mon May 16 2022 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-202
- Ensure that condition in __glibc_fortify is a constant (#2086853)

* Tue May 10 2022 Arjun Shankar <arjun@redhat.com> - 2.28-201
- Add missing MACRON to EBCDIC character sets (#1961109)

* Wed May  4 2022 DJ Delorie <dj@redhat.com> - 2.28-200
- Fix glob defects on certain XFS filesystems (#1982608)

* Tue Apr 26 2022 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-199
- Fix fortify false positive with mbsrtowcs and mbsnrtowcs (#2072329).

* Fri Apr 22 2022 Carlos O'Donell <carlos@redhat.com> - 2.28-198
- Fix multi-threaded popen defect leading to segfault (#2065588)

* Tue Apr 05 2022 Arjun Shankar <arjun@redhat.com> - 2.28-197
- timezone: Fix a test that causes occasional build failure (#2071745)

* Tue Mar 15 2022 Siddhesh Poyarekar <siddhesh@redhat.com> 2.28-196
- Synchronize feature guards in fortified functions (#2063042)

* Mon Mar 14 2022 Florian Weimer <fweimer@redhat.com> - 2.28-195
- nss: Avoid clobbering errno in get*ent via dlopen (#2063712)

* Fri Mar 11 2022 Siddhesh Poyarekar <siddhesh@redhat.com> 2.28-194
- Enable support for _FORTIFY_SOURCE=3 for gcc 12 and later (#2033684)

* Wed Mar 9 2022 DJ Delorie <dj@redhat.com> - 2.28-193
- memory operation A64FX SVE performance improvement (#2037416)

* Mon Mar 07 2022 Arjun Shankar <arjun@redhat.com> - 2.28-192
- Move build-locale-archive to glibc-all-langpacks (#2057513)

* Mon Mar 07 2022 Arjun Shankar <arjun@redhat.com> - 2.28-191
- Fix build-locale-archive to handle symbolic links (#2054790)

* Fri Mar 04 2022 Arjun Shankar <arjun@redhat.com> - 2.28-190
- Reduce installed size of some langpacks by de-duplicating LC_CTYPE (#2054790)
- Fix localedef so it can handle symbolic links when generating locale-archive.

* Thu Jan 27 2022 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-189
- CVE-2021-3999: getcwd: align stack on clone in aarch64 and fix a memory leak
  (#2032281)

* Tue Jan 25 2022 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-188
- CVE-2022-23218, CVE-2022-23219: Fix buffer overflows in sunrpc clnt_create
  for "unix" and svcunix_create (#2045063).

* Mon Jan 24 2022 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-187
- CVE-2021-3999: getcwd: Set errno to ERANGE for size == 1 (#2032281)

* Fri Jan 21 2022 Carlos O'Donell <carlos@redhat.com> - 2.28-186
- Fix pthread_once regression with C++ exceptions (#2007327)

* Thu Jan 20 2022 DJ Delorie <dj@redhat.com> - 2.28-185
- Adjust to rpm's find-debuginfo.sh changes, to keep stripping binaries (#1661513)

* Fri Jan  7 2022 Florian Weimer <fweimer@redhat.com> - 2.28-184
- Conversion from ISO-2022-JP-3 may emit spurious NUL character (#2033655)

* Fri Jan  7 2022 Florian Weimer <fweimer@redhat.com> - 2.28-183
- aarch64: A64FX optimizations break "sve=off" guest mode (#2036955)

* Fri Jan  7 2022 Patsy Griffin <patsy@redhat.com> - 2.28-182
- Handle truncated timezones from tzcode-2021d and later. (#2033648)

* Tue Jan  4 2022 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-181
- Weaken dependency of glibc on glibc-gconv-extra (#2015768)

* Mon Dec 13 2021 Florian Weimer <fweimer@redhat.com> - 2.28-180
- Do not install /usr/lib/debug/usr/bin/ld.so.debug (#2023420)

* Fri Dec 10 2021 Florian Weimer <fweimer@redhat.com> - 2.28-179
- Add /usr/bin/ld.so --list-diagnostics (#2023420)

* Fri Dec 10 2021 Carlos O'Donell <carlos@redhat.com> - 2.28-178
- Preliminary support for new IBM zSeries hardware (#1984802)

* Fri Dec 10 2021 Carlos O'Donell <carlos@redhat.com> - 2.28-177
- Fix --with and --without builds for benchtests and bootstrap (#2020989)

* Wed Dec  1 2021 Florian Weimer <fweimer@redhat.com> - 2.28-176
- A64FX memcpy/memmove/memset optimizations (#1929928)

* Tue Nov 30 2021 Florian Weimer <fweimer@redhat.com> - 2.28-175
- Fix dl-tls.c assert failure with pthread_create & dlopen (#1991001)
- Fix x86_64 TLS lazy binding with auditors (#1950056)

* Thu Nov 25 2021 Arjun Shankar <arjun@redhat.com> - 2.28-174
- Introduce new glibc-doc.noarch subpackage (#2021671)
- Move the reference manual info pages from glibc-devel to glibc-doc
- Move debugger interface documentation from glibc to glibc-doc
- Remove unnecessary README, INSTALL, NEWS files from glibc
- Remove unnecessary README.timezone and gai.conf files from glibc-common

* Wed Nov 17 2021 Patsy Griffin <patsy@redhat.com> - 2.28-173
- Add new English-language 12 hour time locale en_US@ampm.UTF-8 (#2000374)

* Tue Nov 16 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-172
- Build build-locale-archive with -static-pie when supported (#1965377)

* Wed Nov  10 2021 DJ Delorie <dj@redhat.com> - 2.28-171
- elf: Always set link map in _dl_init_paths (#1934162)

* Wed Nov 10 2021 Arjun Shankar <arjun@redhat.com> - 2.28-170
- x86: Properly disable XSAVE related features when its use is disabled via
  tunables (#1937515)

* Wed Nov 10 2021 Arjun Shankar <arjun@redhat.com> - 2.28-169
- s390: Use long branches across object boundaries (#2021452)

* Fri Oct 29 2021 Arjun Shankar <arjun@redhat.com> - 2.28-168
- Optimize memcmp, strcpy, and stpcpy for IBM POWER10 (#1983203)

* Wed Oct 13 2021 Arjun Shankar <arjun@redhat.com> - 2.28-167
- malloc: Initiate tcache shutdown even without allocations (#1977614)

* Wed Oct 13 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-166
- Fix debuginfo location for gconv-extra and make glibc Require it (#1971664).

* Wed Oct  6 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-165
- Split extra gconv modules into a separate package (#1971664).

* Mon Aug  9 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-164
- librt: fix NULL pointer dereference (#1966472).

* Mon Aug  9 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-163
- CVE-2021-33574: Deep copy pthread attribute in mq_notify (#1966472)

* Thu Jul  8 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-162
- CVE-2021-35942: wordexp: handle overflow in positional parameter number
  (#1979127)

* Fri Jun 18 2021 Carlos O'Donell <carlos@redhat.com> - 2.28-161
- Improve POWER10 performance with POWER9 fallbacks (#1956357)

* Mon May 31 2021 Arjun Shankar <arjun@redhat.com> - 2.28-160
- Backport POWER10 optimized rawmemchr for ppc64le (#1956357)

* Thu May 27 2021 Arjun Shankar <arjun@redhat.com> - 2.28-159
- Backport additional ifunc optimizations for ppc64le (#1956357)

* Thu Apr 22 2021 Florian Weimer <fweimer@redhat.com> - 2.28-158
- Rebuild with new binutils (#1946518)

* Wed Apr 14 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-157
- Consistently SXID_ERASE tunables in sxid binaries (#1934155)

* Wed Mar 31 2021 DJ Delorie <dj@redhat.com> - 2.28-156
- Backport ifunc optimizations for glibc for ppc64le (#1918719)

* Wed Mar 24 2021 Arjun Shankar <arjun@redhat.com> - 2.28-155
- CVE-2021-27645: nscd: Fix double free in netgroupcache (#1927877)

* Thu Mar 18 2021 Carlos O'Donell <carlos@redhat.com> - 2.28-154
- Add IPPROTO_ETHERNET, IPPROTO_MPTCP, and INADDR_ALLSNOOPERS_GROUP defines
  (#1930302)

* Thu Mar 18 2021 Carlos O'Donell <carlos@redhat.com> - 2.28-153
- Support SEM_STAT_ANY via semctl. Return EINVAL for unknown commands to semctl,
  msgctl, and shmctl. (#1912670)

* Tue Mar 16 2021 Patsy Griffin <patsy@redhat.com> - 2.28-152
- Update syscall-names.list to 5.7, 5.8, 5.9, 5.10 and 5.11. (#1871386)

* Mon Mar 15 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-151
- CVE-2019-9169: Fix buffer overread in regexec.c (#1936864).

* Mon Mar 15 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-150
- Rebuild glibc to update security markup metadata (#1935128)

* Mon Mar 15 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-149
- Fix NSS files and compat service upgrade defect (#1932770).

* Fri Feb  5 2021 Florian Weimer <fweimer@redhat.com> - 2.28-148
- CVE-2021-3326: iconv assertion failure in ISO-2022-JP-3 decoding (#1924919)

* Wed Jan 20 2021 Florian Weimer <fweimer@redhat.com> - 2.28-147
- x86-64: Fix FMA4 math routine selection after bug 1817513 (#1918115)

* Mon Jan 18 2021 Siddhesh Poyarekar <siddhesh@redhat.com> - 2.28-146
- CVE-2019-25013:Fix buffer overrun in EUC-KR conversion module (#1912544)

* Mon Jan  4 2021 Florian Weimer <fweimer@redhat.com> - 2.28-145
- Update glibc-hwcaps fix from upstream (#1817513)

* Tue Dec 15 2020 Florian Weimer <fweimer@redhat.com> - 2.28-144
- Support running libc.so.6 as a main program in more cases (#1882466)

* Thu Dec 10 2020 Florian Weimer <fweimer@redhat.com> - 2.28-142
- Add glibc-hwcaps support (#1817513)
- Implement DT_AUDIT support (#1871385)

* Mon Nov 30 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-141
- Update Intel CET support (#1855790)

* Tue Nov 10 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-140
- Fix calling lazily-bound SVE-using functions on AArch64 (#1893662)

* Tue Nov 10 2020 Arjun Shankar <arjun@redhat.com> - 2.28-139
- CVE-2016-10228, CVE-2020-27618: Fix infinite loops in iconv (#1704868,
  #1894669)

* Fri Nov  6 2020 Florian Weimer <fweimer@redhat.com> - 2.28-138
- Avoid comments after %%endif in the RPM spec file (#1894340)

* Fri Oct 30 2020 Florian Weimer <fweimer@redhat.com> - 2.28-137
- x86: Further memcpy optimizations for AMD Zen (#1880670)

* Tue Oct 27 2020 DJ Delorie <dj@redhat.com> - 2.28-136
- Allow __getauxval in testsuite check (#1856398)

* Wed Oct 21 2020 DJ Delorie <dj@redhat.com> - 2.28-135
- Use -moutline-atomics for aarch64 (#1856398)

* Tue Oct 20 2020 Florian Weimer <fweimer@redhat.com> - 2.28-134
- resolv: Handle DNS transaction ID collisions (#1868106)

* Tue Oct 20 2020 Florian Weimer <fweimer@redhat.com> - 2.28-133
- x86: Update auto-tuning of memcpy non-temporal threshold (#1880670)

* Mon Oct 5 2020 DJ Delorie <dj@redhat.com> - 2.28-132
- Fix fgetsgent_r data corruption bug (#1871397)

* Fri Oct 02 2020 Patsy Griffin <patsy@redhat.com> - 2.28-131
- Improve IBM zSeries (s390x) Performance (#1871395)

* Fri Oct 02 2020 Patsy Griffin <patsy@redhat.com> - 2.28-130
- Fix avx2 strncmp offset compare condition check (#1871394)
- Add strncmp and strcmp testcases for page boundary

* Fri Sep 18 2020 Arjun Shankar <arjun@redhat.com> - 2.28-129
- Improve IBM POWER9 architecture performance (#1871387)

* Thu Sep 17 2020 Arjun Shankar <arjun@redhat.com> - 2.28-128
- Enable glibc for POWER10 (#1845098)

* Tue Jun 09 2020 Carlos O'Donell <calros@redhat.com> - 2.28-127
- Improve performance of library strstr() function (#1821531)

* Wed May 27 2020 Florian Weimer <fweimer@redhat.com> - 2.28-126
- Do not clobber errno in nss_compat (#1836867)

* Thu May 14 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-125
- Support building rpm under newer versions of Coverity Scan (#1835999)

* Mon May 11 2020 Florian Weimer <fweimer@redhat.com> - 2.28-124
- Enhance memory protection key support on ppc64le (#1642150)

* Thu Apr 23 2020 Florian Weimer <fweimer@redhat.com> - 2.28-123
- Reduce IFUNC resolver usage in libpthread and librt (#1748197)

* Thu Apr  9 2020 DJ Delorie <dj@redhat.com> - 2.28-122
- Math library optimizations for IBM Z (#1780204)
- Additional patch for s_nearbyint.c

* Wed Apr  8 2020 Florian Weimer <fweimer@redhat.com> - 2.28-121
- elf: Assign TLS modid later during dlopen (#1774115)

* Wed Apr  8 2020 Florian Weimer <fweimer@redhat.com> - 2.28-120
- x86-64: Automatically install nss_db.i686 for 32-bit environments (#1807824)

* Tue Apr  7 2020 Florian Weimer <fweimer@redhat.com> - 2.28-119
- ppc64le: Enable protection key support (#1642150)

* Tue Apr  7 2020 Florian Weimer <fweimer@redhat.com> - 2.28-118
- ppc64le: floating-point status and exception optimizations (#1783303)

* Fri Apr  3 2020 Patsy Griffin <patsy@redhat.com> - 2.28-117
- Update to Linux 5.6 syscall-names.list. (#1810224)

* Fri Apr  3 2020 Patsy Griffin <patsy@redhat.com> - 2.28-116
- CVE-2020-1751: Fix an array overflow in backtrace on PowerPC. (#1813399)

* Fri Apr  3 2020 Patsy Griffin <patsy@redhat.com> - 2.28-115
- CVE:2020-1752: Fix a use after free in glob when expanding ~user. (#1813398)

* Fri Apr  3 2020 Patsy Griffin <patsy@redhat.com> - 2.28-114
- CVE-2020-10029: Prevent stack corruption from crafted input in cosl, sinl,
  sincosl, and tanl function. (#1811796)

* Thu Apr  2 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-113
- Improve elf/ and nptl/ testsuites (#1810223)

* Thu Apr  2 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-112
- Fix resource leak in getaddrinfo (#1810146)

* Thu Apr  2 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-111
- Protect locale archive against corruption (#1784525)

* Thu Apr  2 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-110
- Properly handle signed vs. unsigned values in mallopt (#1784520)

* Thu Apr  2 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-109
- Update and harmonize locale names with CLDR (#1757354)

* Thu Apr  2 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-108
- Fix filter and auxiliary filter implementation (#1812756)

* Thu Apr  2 2020 Carlos O'Donell <carlos@redhat.com> - 2.28-107
- Handle .dynstr located in separate segment (#1774114)

* Fri Mar 27 2020 Patsy Griffin <patsy@redhat.com> - 2.28-106
- Disable vtable validation for pre-2.1 interposed handles (#1775819)

* Fri Mar 27 2020 Patsy Griffin <patsy@redhat.com> - 2.28-105
- Define __CORRECT_ISO_CPP_STRING_H_PROTO for Clang. (#1784519)

* Wed Mar 25 2020 DJ Delorie <dj@redhat.com> - 2.28-104
- Math library optimizations for IBM Z (#1780204)

* Wed Mar 25 2020 DJ Delorie <dj@redhat.com> - 2.28-103
- Filter "ignore" autofs mount entries in getmntent (#1743445)

* Wed Mar 25 2020 DJ Delorie <dj@redhat.com> - 2.28-102
- Fix /etc/resolv.conf reloading defects (#1810142)

* Thu Jan 16 2020 Florian Weimer <fweimer@redhat.com> - 2.28-101
- ld.so: Reset GL (dl_initfirst) pointer on dlopen failure (#1410154)

* Fri Dec 13 2019 Florian Weimer <fweimer@redhat.com> - 2.28-100
- Roll back dynamic linker state on dlopen failure (#1410154)

* Wed Nov 27 2019 Florian Weimer <fweimer@redhat.com> - 2.28-99
- s390x: Fix z15 strstr for patterns crossing pages (#1777241)

* Wed Nov 27 2019 Florian Weimer <fweimer@redhat.com> - 2.28-98
- Rebuild with new rpm (#1654901)

* Fri Nov 22 2019 Florian Weimer <fweimer@redhat.com> - 2.28-97
- Avoid invalid __has_include in <sys/stat.h> (#1775294)

* Fri Nov 22 2019 Florian Weimer <fweimer@redhat.com> - 2.28-96
- x86-64: Ignore LD_PREFER_MAP_32BIT_EXEC in SUID binaries (#1774021)

* Thu Nov 14 2019 DJ Delorie <dj@redhat.com> - 2.28-95
- Fix alignment of TLS variables for tls variant TLS_TCB_AT_TP (#1764214)

* Thu Nov 14 2019 DJ Delorie <dj@redhat.com> - 2.28-94
- Refuse to dlopen PIE objects (#1764223)

* Thu Nov 14 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-93
- Fix C.UTF-8 locale source ellipsis expressions (#1361965)

* Thu Nov 14 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-92
- Fix hangs during malloc tracing (#1764235)

* Thu Nov 14 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-91
- Support moving versioned symbols between sonames (#1764231)

* Wed Nov 13 2019 Florian Weimer <fweimer@redhat.com> - 2.28-90
- Avoid creating stale utmp entries for repeated pututxline (#1749439)

* Wed Nov  6 2019 Florian Weimer <fweimer@redhat.com> - 2.28-89
- Backport more precise tokenizer for installed headers test (#1769304)

* Wed Nov  6 2019 Florian Weimer <fweimer@redhat.com> - 2.28-88
- math: Enable some math builtins for clang in LLVM Toolset (#1764242)

* Wed Nov  6 2019 Florian Weimer <fweimer@redhat.com> - 2.28-87
- Support Fortran vectorized math functions with GCC Toolset 9 (#1764238)

* Wed Nov  6 2019 Florian Weimer <fweimer@redhat.com> - 2.28-86
- aarch64: Support STO_AARCH64_VARIANT_PCS, DT_AARCH64_VARIANT_PCS (#1726638)

* Mon Nov  4 2019 DJ Delorie <dj@redhat.com> - 2.28-85
- Add more test-in-container support (#1747502)

* Fri Nov  1 2019 DJ Delorie <dj@redhat.com> - 2.28-84
- Fix calling getpwent after endpwent (#1747502)

* Fri Nov  1 2019 DJ Delorie <dj@redhat.com> - 2.28-83
- nptl: Avoid fork handler lock for async-signal-safe fork (#1746928)

* Thu Oct 31 2019 DJ Delorie <dj@redhat.com> - 2.28-82
- Call _dl_open_check after relocation (#1682954)

* Thu Oct 31 2019 Arjun Shankar <arjun@redhat.com> - 2.28-81
- Add malloc fastbin tunable (#1764218)

* Thu Oct 31 2019 Arjun Shankar <arjun@redhat.com> - 2.28-80
- Fix race condition in tst-clone3 and add a new ldconfig test,
  tst-ldconfig-bad-aux-cache (#1764226)

* Thu Oct 31 2019 Arjun Shankar <arjun@redhat.com> - 2.28-79
- Remove unwanted whitespace from size lines and account for top chunk in
  malloc_info output (#1735747)

* Wed Oct 30 2019 Arjun Shankar <arjun@redhat.com> - 2.28-78
- Enhance malloc tcache (#1746933)

* Tue Oct 29 2019 Patsy Griffin <patsy@redhat.com> - 2.28-77
- Don't define initgroups in nsswitch.conf (#1747505)

* Mon Oct 28 2019 Patsy Griffin <patsy@redhat.com> - 2.28-76
- libio: Remove codecvt vtable. (#1764241)

* Mon Oct 28 2019 Patsy Griffin <patsy@redhat.com> - 2.28-75
- Implement --preload option for the dynamic linker.(#1747453)

* Mon Oct 28 2019 Patsy Griffin <patsy@redhat.com> - 2.28-74
- Make nsswitch.conf more distribution friendly.
  Improve nscd.conf comments.  (#1747505)

* Fri Oct 25 2019 Patsy Griffin <patsy@redhat.com> - 2.28-73
- Update system call names list to Linux 5.3 (#1764234)

* Mon Jul 22 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-72
- Skip wide buffer handling for legacy stdio handles (#1722215)

* Mon Jul 22 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-71
- Remove copy_file_range emulation (#1724975)

* Mon Jul 22 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-70
- Avoid nscd assertion failure during persistent db check (#1727152)

* Mon Jul 22 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-69
- Fix invalid free under valgrind with libdl (#1717438)

* Thu Jul 18 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-68
- Account for size of locale-archive in rpm package (#1725131)

* Thu Jul 18 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-67
- Reject IP addresses with trailing characters in getaddrinfo (#1727241)

* Fri Jun 14 2019 Florian Weimer <fweimer@redhat.com> - 2.28-66
- Avoid header conflict between <sys/stat.h> and <linux/stat.h> (#1699194)

* Wed Jun 12 2019 Florian Weimer <fweimer@redhat.com> - 2.28-65
- glibc-all-langpacks: Do not delete locale archive during update (#1717347)
- Do not mark /usr/lib/locale/locale-archive as a configuration file
  because it is always automatically overwritten by build-locale-archive.

* Mon Jun 10 2019 DJ Delorie <dj@redhat.com> - 2.28-64
- Avoid ABI exposure of the NSS service_user type (#1710894)

* Thu Jun  6 2019 Patsy Griffin Franklin <patsy@redhat.com> - 2.28-63
- Enable full ICMP errors for UDP DNS sockets. (#1670043)

* Mon Jun  3 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-62
- Convert post-install binary to rpm lua scriptlet (#1639346)

* Mon Jun  3 2019 Florian Weimer <fweimer@redhat.com> - 2.28-61
- Fix crash during wide stream buffer flush (#1710478)

* Fri May 31 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-60
- Add PF_XDP, AF_XDP and SOL_XDP from Linux 4.18 (#1706777)

* Wed May 22 2019 DJ Delorie <dj@redhat.com> - 2.28-59
- Add .gdb_index to debug information (#1612448)

* Wed May 22 2019 DJ Delorie <dj@redhat.com) - 2.28-58
- iconv, localedef: avoid floating point rounding differences (#1691528)

* Wed May 22 2019 Arjun Shankar <arjun@redhat.com> - 2.28-57
- locale: Add LOCPATH diagnostics to the locale program (#1701605)

* Fri May 17 2019 Patsy Griffin Franklin <patsy@redhat.com> - 2.28-56
- Fix hang in pldd.  (#1702539)

* Mon May 13 2019 Florian Weimer <fweimer@redhat.com> - 2.28-55
- s390x string function improvements (#1659438)

* Thu May  2 2019 Patsy Griffin Franklin <patsy@redhat.com> - 2.28-54
- Fix test suite failures due to race conditions in posix/tst-spawn
  spawned processes. (#1659512)

* Wed May  1 2019 DJ Delorie <dj@redhat.com> - 2.28-53
- Add missing CFI data to __mpn_* functions on ppc64le (#1658901)

* Fri Apr 26 2019 Arjun Shankar <arjun@redhat.com> - 2.28-52
- intl: Do not return NULL on asprintf failure in gettext (#1663035)

* Fri Apr 26 2019 Florian Weimer <fweimer@redhat.com> - 2.28-51
- Increase BIND_NOW coverage (#1639343)

* Tue Apr 23 2019 Carlos O'Donell <carlos@redhat.com> - 2.28-50
- Fix pthread_rwlock_trywrlock and pthread_rwlock_tryrdlock stalls (#1659293)

* Tue Apr 23 2019 Arjun Shankar <arjun@redhat.com> - 2.28-49
- malloc: Improve bad chunk detection (#1651283)

* Mon Apr 22 2019 Patsy Griffin Franklin <patsy@redhat.com> - 2.28-48
- Add compiler barriers around modifications of the robust mutex list
  for pthread_mutex_trylock. (#1672773)

* Tue Apr 16 2019 DJ Delorie <dj@redhat.com> - 2.28-47
- powerpc: Only enable HTM if kernel supports PPC_FEATURE2_HTM_NOSC (#1651742)

* Fri Apr 12 2019 Florian Weimer <fweimer@redhat.com> - 2.28-46
- Only build libm with -fno-math-errno (#1664408)

* Tue Apr  2 2019 Florian Weimer <fweimer@redhat.com> - 2.28-45
- ja_JP: Add new Japanese Era name (#1577438)

* Wed Mar  6 2019 Florian Weimer <fweimer@redhat.com> - 2.28-44
- math: Add XFAILs for some IBM 128-bit long double fma tests (#1623537)

* Fri Mar  1 2019 Florian Weimer <fweimer@redhat.com> - 2.28-43
- malloc: realloc ncopies integer overflow (#1662843)

* Fri Dec 14 2018 Florian Weimer <fweimer@redhat.com> - 2.28-42
- Fix rdlock stall with PREFER_WRITER_NONRECURSIVE_NP (#1654872)

* Fri Dec 14 2018 Florian Weimer <fweimer@redhat.com> - 2.28-41
- malloc: Implement double-free check for the thread cache (#1642094)

* Thu Dec 13 2018 Florian Weimer <fweimer@redhat.com> - 2.28-40
- Add upstream test case for CVE-2018-19591 (#1654010)

* Thu Dec 13 2018 Florian Weimer <fweimer@redhat.com> - 2.28-39
- Add GCC dependency for new inline string functions on ppc64le (#1652932)

* Sat Dec 01 2018 Carlos O'Donell <carlos@redhat.com> - 2.28-38
- Add requires on explicit glibc version for glibc-nss-devel (#1649890)

* Fri Nov 30 2018 Carlos O'Donell <carlos@redhat.com> - 2.28-37
- Fix data race in dynamic loader when using LD_AUDIT (#1635779)

* Wed Nov 28 2018 Florian Weimer <fweimer@redhat.com> - 2.28-36
- CVE-2018-19591: File descriptor leak in if_nametoindex (#1654010)

* Mon Nov 26 2018 Florian Weimer <fweimer@redhat.com> - 2.28-35
- Do not use parallel make for building locales (#1652229)

* Tue Nov 20 2018 Florian Weimer <fweimer@redhat.com> - 2.28-34
- support: Print timestamps in timeout handler (#1651274)

* Tue Nov 20 2018 Florian Weimer <fweimer@redhat.com> - 2.28-33
- Increase test timeout for  libio/tst-readline (#1638520)

* Tue Nov 20 2018 Florian Weimer <fweimer@redhat.com> - 2.28-32
- Fix tzfile low-memory assertion failure (#1650571)

* Tue Nov 20 2018 Florian Weimer <fweimer@redhat.com> - 2.28-31
- Add newlines in __libc_fatal calls (#1650566)

* Tue Nov 20 2018 Florian Weimer <fweimer@redhat.com> - 2.28-30
- nscd: Fix use-after-free in addgetnetgrentX (#1650563)

* Tue Nov 20 2018 Florian Weimer <fweimer@redhat.com> - 2.28-29
- Update syscall names to Linux 4.19 (#1650560)

* Tue Nov 13 2018 Florian Weimer <fweimer@redhat.com> - 2.28-28
- kl_GL: Fix spelling of Sunday, should be "sapaat" (#1645597)

* Tue Nov 13 2018 Florian Weimer <fweimer@redhat.com> - 2.28-27
- Fix x86 CPU flags analysis for string function selection (#1641982)

* Fri Nov  9 2018 Florian Weimer <fweimer@redhat.com> - 2.28-26
- Reduce RAM requirements for stdlib/test-bz22786 (#1638523)

* Fri Nov  9 2018 Florian Weimer <fweimer@redhat.com> - 2.28-25
- x86: Improve enablement for 32-bit code using CET (#1645601)

* Fri Nov  9 2018 Florian Weimer <fweimer@redhat.com> - 2.28-24
- Fix crash in getaddrinfo_a when thread creation fails (#1646379)

* Fri Nov  9 2018 Florian Weimer <fweimer@redhat.com> - 2.28-23
- Fix race in pthread_mutex_lock related to PTHREAD_MUTEX_ELISION_NP (#1645604)

* Fri Nov  9 2018 Florian Weimer <fweimer@redhat.com> - 2.28-22
- Fix misreported errno on preadv2/pwritev2 (#1645596)

* Fri Nov  9 2018 Florian Weimer <fweimer@redhat.com> - 2.28-21
- Fix posix/tst-spawn4-compat test case (#1645593)

* Fri Nov  9 2018 Florian Weimer <fweimer@redhat.com> - 2.28-20
- Disable CET for binaries created by older link editors (#1614979)

* Fri Nov  2 2018 Mike FABIAN <mfabian@redhat.com> - 2.28-19
- Include Esperanto (eo) in glibc-all-langpacks (#1644303)

* Thu Sep 27 2018 Florian Weimer <fweimer@redhat.com> - 2.28-18
- stdlib/tst-setcontext9 test suite failure on ppc64le (#1623536)

* Wed Sep 26 2018 Florian Weimer <fweimer@redhat.com> - 2.28-17
- Add missing ENDBR32 in start.S (#1631730)

* Wed Sep 26 2018 Florian Weimer <fweimer@redhat.com> - 2.28-16
- Fix bug in generic strstr with large needles (#1631722)

* Wed Sep 26 2018 Florian Weimer <fweimer@redhat.com> - 2.28-15
- stdlib/tst-setcontext9 test suite failure (#1623536)

* Wed Sep 26 2018 Florian Weimer <fweimer@redhat.com> - 2.28-14
- gethostid: Missing NULL check for gethostbyname_r (#1631293)

* Wed Sep  5 2018 Carlos O'Donell <carlos@redhat.com> - 2.28-13
- Provide compatibility support for linking against libpthread_nonshared.a
  (#1614439)

* Wed Sep  5 2018 Florian Weimer <fweimer@redhat.com> - 2.28-12
- Add python3-devel build dependency (#1625592)

* Wed Aug 29 2018 Florian Weimer <fweimer@redhat.com> - 2.28-11
- Drop glibc-ldflags.patch and valgrind bug workaround (#1623456)

* Wed Aug 29 2018 Florian Weimer <fweimer@redhat.com> - 2.28-10
- regex: Fix memory overread when pattern contains NUL byte (#1622678)

* Wed Aug 29 2018 Florian Weimer <fweimer@redhat.com> - 2.28-9
- nptl: Fix waiters-after-spinning case in pthread_cond_broadcast (#1622675)

* Tue Aug 14 2018 Florian Weimer <fweimer@redhat.com> - 2.28-8
- nss_files aliases database file stream leak (#1615790)

* Tue Aug 14 2018 Florian Weimer <fweimer@redhat.com> - 2.28-7
- Fix static analysis warning in nscd user name allocation (#1615784)

* Tue Aug 14 2018 Florian Weimer <fweimer@redhat.com> - 2.28-6
- error, error_at_line: Add missing va_end calls (#1615781)

* Mon Aug 13 2018 Carlos O'Donell <carlos@redhat.com> - 2.28-5
- Remove abort() warning in manual (#1577365)

* Fri Aug 10 2018 Florian Weimer <fweimer@redhat.com> - 2.28-4
- Fix regression in readdir64@GLIBC_2.1 compat symbol (#1614253)

* Thu Aug  2 2018 Florian Weimer <fweimer@redhat.com> - 2.28-3
- Log /proc/sysinfo if available (on s390x)

* Thu Aug  2 2018 Florian Weimer <fweimer@redhat.com> - 2.28-2
- Honor %%{valgrind_arches}

* Wed Aug 01 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-43
- Update to glibc 2.28 release tarball:
- Translation updates
- x86/CET: Fix property note parser (swbz#23467)
- x86: Add tst-get-cpu-features-static to $(tests) (swbz#23458)

* Mon Jul 30 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-42
- Auto-sync with upstream branch master,
  commit af86087f02a5522d8801a11d8381e04f95e33162:
- x86/CET: Don't parse beyond the note end
- Fix Linux fcntl OFD locks tests on unsupported kernels
- x86: Populate COMMON_CPUID_INDEX_80000001 for Intel CPUs (swbz#23459)
- x86: Correct index_cpu_LZCNT (swbz#23456)
- Fix string/tst-xbzero-opt if build with gcc head

* Thu Jul 26 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-41
- Build with --enable-cet on x86_64, i686
- Auto-sync with upstream branch master,
  commit cfba5dbb10cc3abde632b46c60c10b2843917035:
- Keep expected behaviour for [a-z] and [A-z] (#1607286)
- Additional ucontext tests
- Intel CET enhancements
- ISO C11 threads support
- Fix out-of-bounds access in IBM-1390 converter (swbz#23448)
- New locale Yakut (Sakha) for Russia (sah_RU) (swbz#22241)
- os_RU: Add alternative month names (swbz#23140)
- powerpc64: Always restore TOC on longjmp (swbz#21895)
- dsb_DE locale: Fix syntax error and add tests (swbz#23208)
- Improve performance of the generic strstr implementation
- regcomp: Fix off-by-one bug in build_equiv_class (swbz#23396)
- Fix out of bounds access in findidxwc (swbz#23442)

* Fri Jul 13 2018 Carlos O'Donell <carlos@redhat.com> - 2.27.9000-40
- Fix file list for glibc RPM packaging (#1601011).

* Wed Jul 11 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-39
- Add POWER9 multilib (downstream only)

* Wed Jul 11 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-38
- Auto-sync with upstream branch master,
  commit 93304f5f7a32f73b551266c5a181db51d97a71e4:
- Install <bits/statx.h> header
- Put the correct Unicode version number 11.0.0 into the generated files

* Wed Jul 11 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-37
- Work around valgrind issue on i686 (#1600034)

* Tue Jul 10 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-36
- Auto-sync with upstream branch master,
  commit fd70af45528d59a00eb3190ef6706cb299488fcd:
- Add the statx function
- regexec: Fix off-by-one bug in weight comparison (#1582229)
- nss_files: Fix re-reading of long lines (swbz#18991)
- aarch64: add HWCAP_ATOMICS to HWCAP_IMPORTANT
- aarch64: Remove HWCAP_CPUID from HWCAP_IMPORTANT
- conform/conformtest.pl: Escape literal braces in regular expressions
- x86: Use AVX_Fast_Unaligned_Load from Zen onwards.

* Fri Jul  6 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-35
- Remove ppc64 multilibs

* Fri Jul 06 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-34
- Auto-sync with upstream branch master,
  commit 3a885c1f51b18852869a91cf59a1b39da1595c7a.

* Thu Jul  5 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-33
- Enable build flags inheritance for nonshared flags

* Wed Jul  4 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-32
- Add annobin annotations to assembler code (#1548438)

* Wed Jul  4 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-31
- Enable -D_FORTIFY_SOURCE=2 for nonshared code

* Mon Jul 02 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-30
- Auto-sync with upstream branch master,
  commit b7b88cea4151d85eafd7ababc2e4b7ae1daeedf5:
- New locale: dsb_DE (Lower Sorbian)

* Fri Jun 29 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-29
- Drop glibc-deprecate_libcrypt.patch.  Variant applied upstream. (#1566464)
- Drop glibc-linux-timespec-header-compat.patch.  Upstreamed.
- Auto-sync with upstream branch master,
  commit e69d994a63afc2d367f286a2a7df28cbf710f0fe.

* Thu Jun 28 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-28
- Drop glibc-rh1315108.patch.  extend_alloca was removed upstream. (#1315108)
- Auto-sync with upstream branch master,
  commit c49e18222e4c40f21586dabced8a49732d946917.

* Thu Jun 21 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-27
- Compatibility fix for <sys/stat.h> and <linux/time.h>

* Thu Jun 21 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-26
- Auto-sync with upstream branch master,
  commit f496b28e61d0342f579bf794c71b80e9c7d0b1b5.

* Mon Jun 18 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-25
- Auto-sync with upstream branch master,
  commit f2857da7cdb65bfad75ee30981f5b2fde5bbb1dc.

* Mon Jun 18 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-24
- Auto-sync with upstream branch master,
  commit 14beef7575099f6373f9a45b4656f1e3675f7372:
- iconv: Make IBM273 equivalent to ISO-8859-1 (#1592270)

* Mon Jun 18 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-23
- Inherit the -msse2 build flag as well (#1592212)

* Fri Jun 01 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-22
- Modernise nsswitch.conf defaults (#1581809)
- Adjust build flags inheritence from redhat-rpm-config
- Auto-sync with upstream branch master,
  commit 104502102c6fa322515ba0bb3c95c05c3185da7a.

* Fri May 25 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-21
- Auto-sync with upstream branch master,
  commit c1dc1e1b34873db79dfbfa8f2f0a2abbe28c0514.

* Wed May 23 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-20
- Auto-sync with upstream branch master,
  commit 7f9f1ecb710eac4d65bb02785ddf288cac098323:
- CVE-2018-11237: Buffer overflow in __mempcpy_avx512_no_vzeroupper (#1581275)
- Drop glibc-rh1452750-allocate_once.patch,
  glibc-rh1452750-libidn2.patch.  Applied upstream.

* Wed May 23 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-19
- Auto-sync with upstream branch master,
  commit 8f145c77123a565b816f918969e0e35ee5b89153.

* Thu May 17 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-18
- Do not run telinit u on upgrades (#1579225)
- Auto-sync with upstream branch master,
  commit 632a6cbe44cdd41dba7242887992cdca7b42922a.

* Fri May 11 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-17
- Avoid exporting some Sun RPC symbols with default versions (#1577210)
- Inherit the -mstackrealign flag if it is set
- Inherit compiler flags in the original order
- Auto-sync with upstream branch master,
  commit 89aacb513eb77549a29df2638913a0f8178cf3f5:
- CVE-2018-11236: realpath: Fix path length overflow (#1581270, swbz#22786)

* Fri May 11 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-16
- Use /usr/bin/python3 for benchmarks scripts (#1577223)

* Thu Apr 19 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-15
- Auto-sync with upstream branch master,
  commit 0085be1415a38b40a5a1a12e49368498f1687380.

* Mon Apr 09 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-14
- Auto-sync with upstream branch master,
  commit 583a27d525ae189bdfaa6784021b92a9a1dae12e.

* Thu Mar 29 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-13
- Auto-sync with upstream branch master,
  commit d39c0a459ef32a41daac4840859bf304d931adab:
- CVE-2017-18269: memory corruption in i386 memmove (#1580934)

* Mon Mar 19 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-12
- Auto-sync with upstream branch master,
  commit fbce6f7260c3847f14dfa38f60c9111978fb33a5.

* Fri Mar 16 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-11
- Auto-sync with upstream branch master,
  commit 700593fdd7aef1e36cfa8bad969faab76a6facda.

* Wed Mar 14 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-10
- Auto-sync with upstream branch master,
  commit 7108f1f944792ac68332967015d5e6418c5ccc88.

* Mon Mar 12 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-9
- Auto-sync with upstream branch master,
  commit da6d4404ecfd7eacba8c096b0761a5758a59da4b.

* Tue Mar  6 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-8
- Enable annobin annotations (#1548438)

* Thu Mar 01 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-7
- Auto-sync with upstream branch master,
  commit 1a2f44a848663036c8a14671fe0faa3fed0b2a25:
- Remove spurios reference to libpthread_nonshared.a

* Thu Mar 01 2018 Florian Weimer <fweimer@redhat.com> - 2.27.9000-6
- Switch back to upstream master branch
- Drop glibc-rh1013801.patch, applied upstream.
- Drop glibc-fedora-nptl-linklibc.patch, no longer needed.
- Auto-sync with upstream branch master,
  commit bd60ce86520b781ca24b99b2555e2ad389bbfeaa.

* Wed Feb 28 2018 Florian Weimer <fweimer@redhat.com> - 2.27-5
- Inherit as many flags as possible from redhat-rpm-config (#1550914)

* Mon Feb 19 2018 Richard W.M. Jones <rjones@redhat.com> - 2.27-4
- riscv64: Add symlink from /usr/lib64/lp64d -> /usr/lib64 for ABI compat.
- riscv64: Disable valgrind smoke test on this architecture.

* Wed Feb 14 2018 Florian Weimer <fweimer@redhat.com> - 2.27-3
- Spec file cleanups:
  - Remove %%defattr(-,root,root)
  - Use shell to run ldconfig %%transfiletrigger
  - Move %%transfiletrigger* to the glibc-common subpackage
  - Trim changelog
  - Include ChangeLog.old in the source RPM

* Wed Feb  7 2018 Florian Weimer <fweimer@redhat.com> - 2.27-2.1
- Linux: use reserved name __key in pkey_get (#1542643)
- Auto-sync with upstream branch release/2.27/master,
  commit 56170e064e2b21ce204f0817733e92f1730541ea.

* Wed Feb 07 2018 Fedora Release Engineering <releng@fedoraproject.org>
- Rebuilt for https://fedoraproject.org/wiki/Fedora_28_Mass_Rebuild

* Mon Feb 05 2018 Carlos O'Donell <carlos@redhat.com> - 2.27-1
- Update to released glibc 2.27.
- Auto-sync with upstream branch master,
  commit 23158b08a0908f381459f273a984c6fd328363cb.

* Tue Jan 30 2018 Richard W.M. Jones <rjones@redhat.com> - 2.26.9000-52
- Disable -fstack-clash-protection on riscv64:
  not supported even by GCC 7.3.1 on this architecture.

* Mon Jan 29 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-51
- Explicitly run ldconfig in the buildroot
- Do not run ldconfig from scriptlets
- Put triggers into the glibc-common package, do not pass arguments to ldconfig

* Mon Jan 29 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-50
- Auto-sync with upstream branch master,
  commit cdd14619a713ab41e26ba700add4880604324dbb:
- libnsl: Turn remaining symbols into compat symbols (swbz#22701)
- be_BY, be_BY@latin, lt_LT, el_CY, el_GR, ru_RU, ru_UA, uk_UA:
  Add alternative month names (swbz#10871)
- x86: Revert Intel CET changes to __jmp_buf_tag (swbz#22743)
- aarch64: Revert the change of the __reserved member of mcontext_t

* Mon Jan 29 2018 Igor Gnatenko <ignatenkobrain@fedoraproject.org> - 2.26.9000-49
- Add file triggers to do ldconfig calls automatically

* Mon Jan 22 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-48
- Auto-sync with upstream branch master,
  commit 21c0696cdef617517de6e25711958c40455c554f:
- locale: Implement alternative month names (swbz#10871)
- locale: Change month names for pl_PL (swbz#10871)

* Mon Jan 22 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-47
- Unconditionally build without libcrypt

* Fri Jan 19 2018 Björn Esser <besser82@fedoraproject.org> - 2.26.9000-46
- Remove deprecated libcrypt, gets replaced by libxcrypt
- Add applicable Requires on libxcrypt

* Fri Jan 19 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-45
- Drop static PIE support on aarch64.  It leads to crashes at run time.
- Remove glibc-rpcgen subpackage.  See rpcsvc-proto.  (#1531540)

* Fri Jan 19 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-44
- Correct the list of static PIE architectures (#1247050)
- glibc_post_upgrade: Remove process restart logic
- glibc_post_upgrade: Integrate into the build process
- glibc_post_upgrade: Do not clean up tls subdirectories
- glibc_post_upgrade: Drop ia64 support
- Remove architecture-specific symbolic link for iconvconfig
- Auto-sync with upstream branch master,
  commit 4612268a0ad8e3409d8ce2314dd2dd8ee0af5269:
- powerpc: Fix syscalls during early process initialization (swbz#22685)

* Fri Jan 19 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-43
- Enable static PIE support on i386, x86_64 (#1247050)
- Remove add-on support (already gone upstream)
- Rework test suite status reporting
- Auto-sync with upstream branch master,
  commit 64f63cb4583ecc1ba16c7253aacc192b6d088511:
- malloc: Fix integer overflows in memalign and malloc functions (swbz#22343)
- x86-64: Properly align La_x86_64_retval to VEC_SIZE (swbz#22715)
- aarch64: Update bits/hwcap.h for Linux 4.15
- Add NT_ARM_SVE to elf.h

* Wed Jan 17 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-42
- CVE-2017-14062, CVE-2016-6261, CVE-2016-6263:
  Use libidn2 for IDNA support (#1452750)

* Mon Jan 15 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-41
- CVE-2018-1000001: Make getcwd fail if it cannot obtain an absolute path
  (#1533837)
- elf: Synchronize DF_1_* flags with binutils (#1439328)
- Auto-sync with upstream branch master,
  commit 860b0240a5645edd6490161de3f8d1d1f2786025:
- aarch64: fix static pie enabled libc when main is in a shared library
- malloc: Ensure that the consolidated fast chunk has a sane size

* Fri Jan 12 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-40
- libnsl: Do not install libnsl.so, libnsl.a (#1531540)
- Use unversioned Supplements: for langpacks (#1490725)
- Auto-sync with upstream branch master,
  commit 9a08a366a7e7ddffe62113a9ffe5e50605ea0924:
- hu_HU locale: Avoid double space (swbz#22657)
- math: Make default libc_feholdsetround_noex_ctx use __feholdexcept
  (swbz#22702)

* Thu Jan 11 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-39
- nptl: Open libgcc.so with RTLD_NOW during pthread_cancel (#1527887)
- Introduce libnsl subpackage and remove NIS headers (#1531540)
- Use versioned Obsoletes: for libcrypt-nss.
- Auto-sync with upstream branch master,
  commit 08c6e95234c60a5c2f37532d1111acf084f39345:
- nptl: Add tst-minstack-cancel, tst-minstack-exit (swbz#22636)
- math: ldbl-128ibm log1pl (-qNaN) spurious "invalid" exception (swbz#22693)

* Wed Jan 10 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-38
- nptl: Fix stack guard size accounting (#1527887)
- Remove invalid Obsoletes: on glibc-header provides
- Require python3 instead of python during builds
- Auto-sync with upstream branch master,
  commit 09085ede12fb9650f286bdcd805609ae69f80618:
- math: ldbl-128ibm lrintl/lroundl missing "invalid" exceptions (swbz#22690)
- x86-64: Add sincosf with vector FMA

* Mon Jan  8 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-37
- Add glibc-rpcgen subpackage, until the replacement is packaged (#1531540)

* Mon Jan 08 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-36
- Auto-sync with upstream branch master,
  commit 579396ee082565ab5f42ff166a264891223b7b82:
- nptl: Add test for callee-saved register restore in pthread_exit
- getrlimit64: fix for 32-bit configurations with default version >= 2.2
- elf: Add linux-4.15 VDSO hash for RISC-V
- elf: Add RISC-V dynamic relocations to elf.h
- powerpc: Fix error message during relocation overflow
- prlimit: Replace old_rlimit RLIM64_INFINITY with RLIM_INFINITY (swbz#22678)

* Fri Jan 05 2018 Florian Weimer <fweimer@redhat.com> - 2.26.9000-35
- Remove sln (#1531546)
- Remove Sun RPC interfaces (#1531540)
- Rebuild with newer GCC to fix pthread_exit stack unwinding issue (#1529549)
- Auto-sync with upstream branch master,
  commit f1a844ac6389ea4e111afc019323ca982b5b027d:
- CVE-2017-16997: elf: Check for empty tokens before DST expansion (#1526866)
- i386: In makecontext, align the stack before calling exit (swbz#22667)
- x86, armhfp: sync sys/ptrace.h with Linux 4.15 (swbz#22433)
- elf: check for rpath emptiness before making a copy of it
- elf: remove redundant is_path argument
- elf: remove redundant code from is_dst
- elf: remove redundant code from _dl_dst_substitute
- scandir: fix wrong assumption about errno (swbz#17804)
- Deprecate external use of libio.h and _G_config.h

* Fri Dec 22 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-34
- Auto-sync with upstream branch master,
  commit bad7a0c81f501fbbcc79af9eaa4b8254441c4a1f:
- copy_file_range: New function to copy file data
- nptl: Consolidate pthread_{timed,try}join{_np}
- nptl: Implement pthread_self in libc.so (swbz#22635)
- math: Provide a C++ version of iseqsig (swbz#22377)
- elf: remove redundant __libc_enable_secure check from fillin_rpath
- math: Avoid signed shift overflow in pow (swbz#21309)
- x86: Add feature_1 to tcbhead_t (swbz#22563)
- x86: Update cancel_jmp_buf to match __jmp_buf_tag (swbz#22563)
- ld.so: Examine GLRO to detect inactive loader (swbz#20204)
- nscd: Fix nscd readlink argument aliasing (swbz#22446)
- elf: do not substitute dst in $LD_LIBRARY_PATH twice (swbz#22627)
- ldconfig: set LC_COLLATE to C (swbz#22505)
- math: New generic sincosf
- powerpc: st{r,p}cpy optimization for aligned strings
- CVE-2017-1000409: Count in expanded path in _dl_init_path (#1524867)
- CVE-2017-1000408: Compute correct array size in _dl_init_paths (#1524867)
- x86-64: Remove sysdeps/x86_64/fpu/s_cosf.S
- aarch64: Improve strcmp unaligned performance

* Wed Dec 13 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-33
- Remove power6 platform directory (#1522675)

* Wed Dec 13 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-32
- Obsolete the libcrypt-nss subpackage (#1525396)
- armhfp: Disable -fstack-clash-protection due to GCC bug (#1522678)
- ppc64: Disable power6 multilib due to GCC bug (#1522675)
- Auto-sync with upstream branch master,
  commit 243b63337c2c02f30ec3a988ecc44bc0f6ffa0ad:
- libio: Free backup area when it not required (swbz#22415)
- math: Fix nextafter and nexttoward declaration (swbz#22593)
- math: New generic cosf
- powerpc: POWER8 memcpy optimization for cached memory
- x86-64: Add sinf with FMA
- x86-64: Remove sysdeps/x86_64/fpu/s_sinf.S
- math: Fix ctanh (0 + i NaN), ctanh (0 + i Inf) (swbz#22568)
- lt_LT locale: Base collation on copy "iso14651_t1" (swbz#22524)
- math: Add _Float32 function aliases
- math: Make cacosh (0 + iNaN) return NaN + i pi/2 (swbz#22561)
- hsb_DE locale: Base collation on copy "iso14651_t1" (swbz#22515)

* Wed Dec 06 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-31
- Add elision tunables.  Drop related configure flag.  (#1383986)
- Auto-sync with upstream branch master,
  commit 37ac8e635a29810318f6d79902102e2e96b2b5bf:
- Linux: Implement interfaces for memory protection keys
- math: Add _Float64, _Float32x function aliases
- math: Use sign as double for reduced case in sinf
- math: fix sinf(NAN)
- math: s_sinf.c: Replace floor with simple casts
- et_EE locale: Base collation on iso14651_t1 (swbz#22517)
- tr_TR locale: Base collation on iso14651_t1 (swbz#22527)
- hr_HR locale: Avoid single code points for digraphs in LC_TIME (swbz#10580)
- S390: Fix backtrace in vdso functions

* Mon Dec 04 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-30
- Add build dependency on bison
- Auto-sync with upstream branch master,
  commit 7863a7118112fe502e8020a0db0fa74fef281f29:
- math: New generic sinf (swbz#5997)
- is_IS locale: Base collation on iso14651_t1 (swbz#22519)
- intl: Improve reproducibility by using bison (swbz#22432)
- sr_RS, bs_BA locales: make collation rules the same as for hr_HR (wbz#22534)
- hr_HR locale: various updates (swbz#10580)
- x86: Make a space in jmpbuf for shadow stack pointer
- CVE-2017-17426: malloc: Fix integer overflow in tcache (swbz#22375)
- locale: make forward accent sorting the default in collating (swbz#17750)

* Wed Nov 29 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-29
- Enable -fstack-clash-protection (#1512531)
- Auto-sync with upstream branch master,
  commit a55430cb0e261834ce7a4e118dd9e0f2b7fb14bc:
- elf: Properly compute offsets of note descriptor and next note (swbz#22370)
- cs_CZ locale: Base collation on iso14651_t1 (swbz#22336)
- Implement the mlock2 function
- Add _Float64x function aliases
- elf: Consolidate link map sorting
- pl_PL locale: Base collation on iso14651_t1 (swbz#22469)
- nss: Export nscd hash function as __nss_hash (swbz#22459)

* Thu Nov 23 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-28
- Auto-sync with upstream branch master,
  commit cccb6d4e87053ed63c74aee063fa84eb63ebf7b8:
- sigwait can fail with EINTR (#1516394)
- Add memfd_create function
- resolv: Fix p_secstodate overflow handling (swbz#22463)
- resolv: Obsolete p_secstodate
- Avoid use of strlen in getlogin_r (swbz#22447)
- lv_LV locale: fix collation (swbz#15537)
- S390: Add cfi information for start routines in order to stop unwinding
- aarch64: Optimized memset for falkor

* Sun Nov 19 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-27
- Auto-sync with upstream branch master,
  commit f6e965ee94b37289f64ecd3253021541f7c214c3:
- powerpc: AT_HWCAP2 bit PPC_FEATURE2_HTM_NO_SUSPEND
- aarch64: Add HWCAP_DCPOP bit
- ttyname, ttyname_r: Don't bail prematurely (swbz#22145)
- signal: Optimize sigrelse implementation
- inet: Check length of ifname in if_nametoindex (swbz#22442)
- malloc: Account for all heaps in an arena in malloc_info (swbz#22439)
- malloc: Add missing arena lock in malloc_info (swbz#22408)
- malloc: Use __builtin_tgmath in tgmath.h with GCC 8 (swbz#21660)
- locale: Replaced unicode sequences in the ASCII printable range
- resolv: More precise checks in res_hnok, res_dnok (swbz#22409, swbz#22412)
- resolv: ns_name_pton should report trailing \ as error (swbz#22413)
- locale: mfe_MU, miq_NI, an_ES, kab_DZ, om_ET: Escape / in d_fmt (swbz#22403)

* Tue Nov 07 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-26
- Auto-sync with upstream branch master,
  commit 6b86036452b9ac47b4ee7789a50f2f37df7ecc4f:
- CVE-2017-15804: glob: Fix buffer overflow during GLOB_TILDE unescaping
- powerpc: Use latest string function optimization for internal function calls
- math: No _Float128 support for ppc64le -mlong-double-64 (swbz#22402)
- tpi_PG locale: Fix wrong d_fmt
- aarch64: Disable lazy symbol binding of TLSDESC
- tpi_PG locale: fix syntax error (swbz#22382)
- i586: Use conditional branches in strcpy.S (swbz#22353)
- ffsl, ffsll: Declare under __USE_MISC, not just __USE_GNU
- csb_PL locale: Fix abmon/mon for March (swbz#19485)
- locale: Various yesstr/nostr/yesexpr/noexpr fixes (swbz#15260, swbz#15261)
- localedef: Add --no-warnings/--warnings option
- powerpc: Replace lxvd2x/stxvd2x with lvx/stvx in P7's memcpy/memmove
- locale: Use ASCII as much as possible in LC_MESSAGES
- Add new locale yuw_PG (swbz#20952)
- malloc: Add single-threaded path to malloc/realloc/calloc/memalloc
- i386: Replace assembly versions of e_powf with generic e_powf.c
- i386: Replace assembly versions of e_log2f with generic e_log2f.c
- x86-64: Add powf with FMA
- x86-64: Add logf with FMA
- i386: Replace assembly versions of e_logf with generic e_logf.c
- i386: Replace assembly versions of e_exp2f with generic e_exp2f.c
- x86-64: Add exp2f with FMA
- i386: Replace assembly versions of e_expf with generic e_expf.c

* Sat Oct 21 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-25
- Auto-sync with upstream branch master,
  commit 797ba44ba27521261f94cc521f1c2ca74f650147:
- math: Add bits/floatn.h defines for more _FloatN / _FloatNx types
- posix: Fix improper assert in Linux posix_spawn (swbz#22273)
- x86-64: Use fxsave/xsave/xsavec in _dl_runtime_resolve (swbz#21265)
- CVE-2017-15670: glob: Fix one-byte overflow (#1504807)
- malloc: Add single-threaded path to _int_free
- locale: Add new locale kab_DZ (swbz#18812)
- locale: Add new locale shn_MM (swbz#13605)

* Fri Oct 20 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-24
- Use make -O to serialize make output
- Auto-sync with upstream branch master,
  commit 63b4baa44e8d22501c433c4093aa3310f91b6aa2:
- sysconf: Fix missing definition of UIO_MAXIOV on Linux (#1504165)
- Install correct bits/long-double.h for MIPS64 (swbz#22322)
- malloc: Fix deadlock in _int_free consistency check
- x86-64: Don't set GLRO(dl_platform) to NULL (swbz#22299)
- math: Add _Float128 function aliases
- locale: Add new locale mjw_IN (swbz#13994)
- aarch64: Rewrite elf_machine_load_address using _DYNAMIC symbol
- powerpc: fix check-before-set in SET_RESTORE_ROUND
- locale: Use U+202F as thousands separators in pl_PL locale (swbz#16777)
- math: Use __f128 to define FLT128_* constants in include/float.h for old GCC
- malloc: Improve malloc initialization sequence (swbz#22159)
- malloc: Use relaxed atomics for malloc have_fastchunks
- locale: New locale ca_ES@valencia (swbz#2522)
- math: Let signbit use the builtin in C++ mode with gcc < 6.x (swbz#22296)
- locale: Place monetary symbol in el_GR, el_CY after the amount (swbz#22019)

* Tue Oct 17 2017 Florian Weimer <fweimer@redhat.com> - 2.26.9000-23
- Switch to .9000 version numbers during development

* Tue Oct 17 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-22
- Auto-sync with upstream branch master,
  commit c38a4bfd596db2be2b9c1f96715bdc833eab760a:
- malloc: Use compat_symbol_reference in libmcheck (swbz#22050)

* Mon Oct 16 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-21
- Auto-sync with upstream branch master,
  commit 596f70134a8f11967c65c1d55a94a3a2718c731d:
- Silence -O3 -Wall warning in malloc/hooks.c with GCC 7 (swbz#22052)
- locale: No warning for non-symbolic character (swbz#22295)
- locale: Allow "" int_curr_Symbol (swbz#22294)
- locale: Fix localedef exit code (swbz#22292)
- nptl: Preserve error in setxid thread broadcast in coredumps (swbz#22153)
- powerpc: Avoid putting floating point values in memory (swbz#22189)
- powerpc: Fix the carry bit on mpn_[add|sub]_n on POWER7 (swbz#22142)
- Support profiling PIE (swbz#22284)

* Wed Oct 11 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-20
- Auto-sync with upstream branch master,
  commit d8425e116cdd954fea0c04c0f406179b5daebbb3:
- nss_files performance issue in multi mode (swbz#22078)
- Ensure C99 and C11 interfaces are available for C++ (swbz#21326)

* Mon Oct 09 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-19
- Move /var/db/Makefile to nss_db (#1498900)
- Auto-sync with upstream branch master,
  commit 645ac9aaf89e3311949828546df6334322f48933:
- openpty: use TIOCGPTPEER to open slave side fd

* Fri Oct 06 2017 Carlos O'Donell <carlos@systemhalted.org> - 2.26.90-18
- Auto-sync with upstream master,
  commit 1e26d35193efbb29239c710a4c46a64708643320.
- malloc: Fix tcache leak after thread destruction (swbz#22111)
- powerpc:  Fix IFUNC for memrchr.
- aarch64: Optimized implementation of memmove for Qualcomm Falkor
- Always do locking when iterating over list of streams (swbz#15142)
- abort: Do not flush stdio streams (swbz#15436)

* Wed Oct 04 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-17
- Move nss_compat to the main glibc package (#1400538)
- Auto-sync with upstream master,
  commit 11c4f5010c58029e73e656d5df4f8f42c9b8e877:
- crypt: Use NSPR header files in addition to NSS header files (#1489339)
- math: Fix yn(n,0) without SVID wrapper (swbz#22244)
- math: Fix log2(0) and log(10) in downward rounding (swbz#22243)
- math: Add C++ versions of iscanonical for ldbl-96, ldbl-128ibm (swbz#22235)
- powerpc: Optimize memrchr for power8
- Hide various internal functions (swbz#18822)

* Sat Sep 30 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-16
- Auto-sync with upstream master,
  commit 1e2bffd05c36a9be30d7092d6593a9e9aa009ada:
- Add IBM858 charset (#1416405)
- Update kernel version in syscall-names.list to 4.13
- Add Linux 4.13 constants to bits/fcntl-linux.h
- Add fcntl sealing interfaces from Linux 3.17 to bits/fcntl-linux.h
- math: New generic powf, log2f, logf
- Fix nearbyint arithmetic moved before feholdexcept (swbz#22225)
- Mark __dso_handle as hidden (swbz#18822)
- Skip PT_DYNAMIC segment with p_filesz == 0 (swbz#22101)
- glob now matches dangling symbolic links (swbz#866, swbz#22183)
- nscd: Release read lock after resetting timeout (swbz#22161)
- Avoid __MATH_TG in C++ mode with -Os for fpclassify (swbz#22146)
- Fix dlclose/exit race (swbz#22180)
- x86: Add SSE4.1 trunc, truncf (swbz#20142)
- Fix atexit/exit race (swbz#14333)
- Use execveat syscall in fexecve (swbz#22134)
- Enable unwind info in libc-start.c and backtrace.c
- powerpc: Avoid misaligned stores in memset
- powerpc: build some IFUNC math functions for libc and libm (swbz#21745)
- Removed redundant data (LC_TIME and LC_MESSAGES) for niu_NZ (swbz#22023)
- Fix LC_TELEPHONE for az_AZ (swbz#22112)
- x86: Add MathVec_Prefer_No_AVX512 to cpu-features (swbz#21967)
- x86: Add x86_64 to x86-64 HWCAP (swbz#22093)
- Finish change from “Bengali” to “Bangla” (swbz#14925)
- posix: fix glob bugs with long login names (swbz#1062)
- posix: Fix getpwnam_r usage (swbz#1062)
- posix: accept inode 0 is a valid inode number (swbz#19971)
- Remove redundant LC_TIME data in om_KE (swbz#22100)
- Remove remaining _HAVE_STRING_ARCH_* definitions (swbz#18858)
- resolv: Fix memory leak with OOM during resolv.conf parsing (swbz#22095)
- Add miq_NI locale for Miskito (swbz#20498)
- Fix bits/math-finite.h exp10 condition (swbz#22082)

* Mon Sep 04 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-15
- Auto-sync with upstream master,
  commit b38042f51430974642616a60afbbf96fd0b98659:
- Implement tmpfile with O_TMPFILE (swbz#21530)
- Obsolete pow10 functions
- math.h: Warn about an already-defined log macro

* Fri Sep 01 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-14
- Build glibc with -O2 (following the upstream default).
- Auto-sync with upstream master,
  commit f4a6be2582b8dfe8adfa68da3dd8decf566b3983:
- malloc: Abort on heap corruption, without a backtrace (swbz#21754)
- getaddrinfo: Return EAI_NODATA for gethostbyname2_r with NO_DATA (swbz#21922)
- getaddrinfo: Fix error handling in gethosts (swbz#21915) (swbz#21922)
- Place $(elf-objpfx)sofini.os last (swbz#22051)
- Various locale fixes (swbz#15332, swbz#22044)

* Wed Aug 30 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-13
- Drop glibc-rh952799.patch, applied upstream (#952799, swbz#22025)
- Auto-sync with upstream master,
  commit 5f9409b787c5758fc277f8d1baf7478b752b775d:
- Various locale fixes (swbz#22022, swbz#22038, swbz#21951, swbz#13805,
  swbz#21971, swbz#21959)
- MIPS/o32: Fix internal_syscall5/6/7 (swbz#21956)
- AArch64: Fix procfs.h not to expose stdint.h types
- iconv_open: Fix heap corruption on gconv_init failure (swbz#22026)
- iconv: Mangle __btowc_fct even without __init_fct (swbz#22025)
- Fix bits/math-finite.h _MSUF_ expansion namespace (swbz#22028)
- Provide a C++ version of iszero that does not use __MATH_TG (swbz#21930)

* Mon Aug 28 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-12
- Auto-sync with upstream master,
  commit 2dba5ce7b8115d6a2789bf279892263621088e74.

* Fri Aug 25 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-11
- Auto-sync with upstream master,
  commit 3d7b66f66cb223e899a7ebc0f4c20f13e711c9e0:
- string/stratcliff.c: Replace int with size_t (swbz#21982)
- Fix tgmath.h handling of complex integers (swbz#21684)

* Thu Aug 24 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-10
- Use an architecture-independent system call list (#1484729)
- Drop glibc-fedora-include-bits-ldbl.patch (#1482105)

* Tue Aug 22 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-9
- Auto-sync with upstream master,
  commit 80f91666fed71fa3dd5eb5618739147cc731bc89.

* Mon Aug 21 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-8
- Auto-sync with upstream master,
  commit a8410a5fc9305c316633a5a3033f3927b759be35:
- Obsolete matherr, _LIB_VERSION, libieee.a.

* Mon Aug 21 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-7
- Auto-sync with upstream master,
  commit 4504783c0f65b7074204c6126c6255ed89d6594e.

* Mon Aug 21 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-6
- Auto-sync with upstream master,
  commit b5889d25e9bf944a89fdd7bcabf3b6c6f6bb6f7c:
- assert: Support types without operator== (int) (#1483005)

* Mon Aug 21 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-5
- Auto-sync with upstream master,
  commit 2585d7b839559e665d5723734862fbe62264b25d:
- Do not use generic selection in C++ mode
- Do not use __builtin_types_compatible_p in C++ mode (#1481205)
- x86-64: Check FMA_Usable in ifunc-mathvec-avx2.h (swbz#21966)
- Various locale fixes (swbz#21750, swbz#21960, swbz#21959, swbz#19852)
- Fix sigval namespace (swbz#21944)
- x86-64: Optimize e_expf with FMA (swbz#21912)
- Adjust glibc-rh827510.patch.

* Wed Aug 16 2017 Tomasz Kłoczko <kloczek@fedoraproject.org> - 2.26-4
- Remove 'Buildroot' tag, 'Group' tag, and '%%clean' section, and don't
  remove the buildroot in '%%install', all per Fedora Packaging Guidelines
  (#1476839)

* Wed Aug 16 2017 Florian Weimer <fweimer@redhat.com> - 2.26.90-3
- Auto-sync with upstream master,
  commit 403143e1df85dadd374f304bd891be0cd7573e3b:
- x86-64: Align L(SP_RANGE)/L(SP_INF_0) to 8 bytes (swbz#21955)
- powerpc: Add values from Linux 4.8 to <elf.h>
- S390: Add new s390 platform z14.
- Various locale fixes (swbz#14925, swbz#20008, swbz#20482, swbz#12349
  swbz#19982, swbz#20756, swbz#20756, swbz#21836, swbz#17563, swbz#16905,
  swbz#21920, swbz#21854)
- NSS: Replace exported NSS lookup functions with stubs (swbz#21962)
- i386: Do not set internal_function
- assert: Suppress pedantic warning caused by statement expression (swbz#21242)
- powerpc: Restrict xssqrtqp operands to Vector Registers (swbz#21941)
- sys/ptrace.h: remove obsolete PTRACE_SEIZE_DEVEL constant (swbz#21928)
- Remove __qaddr_t, __long_double_t
- Fix uc_* namespace (swbz#21457)
- nss: Call __resolv_context_put before early return in get*_r (swbz#21932)
- aarch64: Optimized memcpy for Qualcomm Falkor processor
- manual: Document getcontext uc_stack value on Linux (swbz#759)
- i386: Add <startup.h> (swbz#21913)
- Don't use IFUNC resolver for longjmp or system in libpthread (swbz#21041)
- Fix XPG4.2 bits/sigaction.h namespace (swbz#21899)
- x86-64: Add FMA multiarch functions to libm
- i386: Support static PIE in start.S
- Compile tst-prelink.c without PIE (swbz#21815)
- x86-64: Use _dl_runtime_resolve_opt only with AVX512F (swbz#21871)
- x86: Remove __memset_zero_constant_len_parameter (swbz#21790)

* Wed Aug 16 2017 Florian Weimer <fweimer@redhat.com> - 2.26-2
- Disable multi-arch (IFUNC string functions) on i686 (#1471427)
- Remove nosegneg 32-bit Xen PV support libraries (#1482027)
- Adjust spec file to RPM changes

* Thu Aug 03 2017 Carlos O'Donell <carlos@systemhalted.org> - 2.26-1
- Update to released glibc 2.26.
- Auto-sync with upstream master,
  commit 2aad4b04ad7b17a2e6b0e66d2cb4bc559376617b.
- getaddrinfo: Release resolver context on error in gethosts (swbz#21885)
