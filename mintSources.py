#!/usr/bin/python3

import os
import subprocess
import sys
import configparser
import aptsources.distro
import aptsources.distinfo
from aptsources.sourceslist import SourcesList
import gettext
import threading
import pycurl
from io import BytesIO
from CountryInformation import CountryInformation
import re
import json
import datetime
from urllib.request import urlopen
import requests
from optparse import OptionParser
import locale
import mintcommon
import unicodedata

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkX11', '3.0') # Needed to get xid
from gi.repository import Gtk, Gdk, GdkPixbuf, GdkX11, GObject, Pango

BUTTON_LABEL_MAX_LENGTH = 30

FLAG_PATH = "/usr/share/iso-flag-png/%s.png"
FLAG_SIZE = 16

# i18n
APP = 'mintsources'
LOCALE_DIR = "/usr/share/linuxmint/locale"
locale.bindtextdomain(APP, LOCALE_DIR)
gettext.bindtextdomain(APP, LOCALE_DIR)
gettext.textdomain(APP)
_ = gettext.gettext

# Used as a decorator to run things in the background
def async(func):
    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=func, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread
    return wrapper

# Used as a decorator to run things in the main loop, from another thread
def idle(func):
    def wrapper(*args):
        GObject.idle_add(func, *args)
    return wrapper

def remove_repository_via_cli(line, codename, forceYes):
    if line.startswith("ppa:"):
        user, sep, ppa_name = line.split(":")[1].partition("/")
        ppa_name = ppa_name or "ppa"
        try:
            ppa_info = get_ppa_info_from_lp(user, ppa_name, codename)
            print(_("You are about to remove the following PPA:"))
            if ppa_info["description"] is not None:
                print(" %s" % (ppa_info["description"]))
            print(_(" More info: %s") % str(ppa_info["web_link"]))

            if sys.stdin.isatty():
                if not(forceYes):
                    print(_("Press Enter to continue or Ctrl+C to cancel"))
                    sys.stdin.readline()
            else:
                if not(forceYes):
                        print(_("Unable to prompt for response.  Please run with -y"))
                        sys.exit(1)

        except KeyboardInterrupt as detail:
            print (_("Cancelling..."))
            sys.exit(1)
        except Exception as detail:
            print (_("Cannot get info about PPA: '%s'.") % detail)

        (deb_line, file) = expand_ppa_line(line.strip(), codename)
        deb_line = expand_http_line(deb_line, codename)
        debsrc_line = 'deb-src' + deb_line[3:]

        # Remove the PPA from sources.list.d
        try:
            readfile = open(file, "r")
            content = readfile.read()
            readfile.close()
            content = content.replace(deb_line, "")
            content = content.replace(debsrc_line, "")
            with open(file, "w") as writefile:
                writefile.write(content)

            # If file no longer contains any "deb" instances, delete it as well
            if "deb " not in content:
                os.unlink(file)
        except IOError as detail:
            print (_("failed to remove PPA: '%s'") % detail)

    elif line.startswith("deb ") | line.startswith("http"):
        # Remove the repository from sources.list.d
        file="/etc/apt/sources.list.d/additional-repositories.list"
        try:
            readfile = open(file, "r")
            content = readfile.read()
            readfile.close()
            content = content.replace(expand_http_line(line, codename), "")
            with open(file, "w") as writefile:
                writefile.write(content)

            # If file no longer contains any "deb" instances, delete it as well
            if "deb " not in content:
                os.unlink(file)
        except IOError as detail:
            print (_("failed to remove repository: '%s'") % detail)


def add_repository_via_cli(line, codename, forceYes, use_ppas):

    if line.startswith("ppa:"):
        if use_ppas != "true":
            print(_("Adding PPAs is not supported"))
            sys.exit(1)
        user, sep, ppa_name = line.split(":")[1].partition("/")
        ppa_name = ppa_name or "ppa"
        try:
            ppa_info = get_ppa_info_from_lp(user, ppa_name, codename)
        except Exception as detail:
            print (_("Cannot add PPA: '%s'.") % detail)
            sys.exit(1)

        if "private" in ppa_info and ppa_info["private"]:
            print(_("Adding private PPAs is not supported currently"))
            sys.exit(1)

        print(_("You are about to add the following PPA:"))
        if ppa_info["description"] is not None:
            print(" %s" % (str(ppa_info["description"]).encode("utf-8","ignore")))
        print(_(" More info: %s") % str(ppa_info["web_link"]))

        if sys.stdin.isatty():
            if not(forceYes):
                print(_("Press Enter to continue or Ctrl+C to cancel"))
                sys.stdin.readline()
        else:
            if not(forceYes):
                print(_("Unable to prompt for response.  Please run with -y"))
                sys.exit(1)

        (deb_line, file) = expand_ppa_line(line.strip(), codename)
        deb_line = expand_http_line(deb_line, codename)
        debsrc_line = 'deb-src' + deb_line[3:]

        # Add the key
        short_key = ppa_info["signing_key_fingerprint"][-8:]
        os.system("apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys %s" % short_key)

        # Add the PPA in sources.list.d
        with open(file, "w") as text_file:
            text_file.write("%s\n" % deb_line)
            text_file.write("%s\n" % debsrc_line)
    elif line.startswith("deb ") | line.startswith("http"):
        with open("/etc/apt/sources.list.d/additional-repositories.list", "a") as text_file:
            text_file.write("%s\n" % expand_http_line(line, codename))

def get_ppa_info_from_lp(owner_name, ppa_name, base_codename):
    DEFAULT_KEYSERVER = "hkp://keyserver.ubuntu.com:80/"
    # maintained until 2015
    LAUNCHPAD_PPA_API = 'https://launchpad.net/api/1.0/~%s/+archive/%s'
    # Specify to use the system default SSL store; change to a different path
    # to test with custom certificates.
    LAUNCHPAD_PPA_CERT = "/etc/ssl/certs/ca-certificates.crt"

    lp_url = LAUNCHPAD_PPA_API % (owner_name, ppa_name)
    try:
        json_data = requests.get(lp_url).json()
    except pycurl.error as e:
        raise PPAException("Error reading %s: %s" % (lp_url, e), e)

    # Make sure the PPA supports our base release
    repo_url = "http://ppa.launchpad.net/%s/%s/ubuntu/dists/%s" % (owner_name, ppa_name, base_codename)
    try:
        if (urlopen(repo_url).getcode() == 404):
            raise PPAException(_("This PPA does not support %s") % base_codename)
    except Exception as e:
        print (e)
        raise PPAException(_("This PPA does not support %s") % base_codename)

    return json_data

def encode(s):
    return re.sub("[^a-zA-Z0-9_-]", "_", s)

def expand_ppa_line(abrev, distro_codename):
    # leave non-ppa: lines unchanged
    if not abrev.startswith("ppa:"):
        return (abrev, None)
    # FIXME: add support for dependency PPAs too (once we can get them
    #        via some sort of API, see LP #385129)
    abrev = abrev.split(":")[1]
    ppa_owner = abrev.split("/")[0]
    try:
        ppa_name = abrev.split("/")[1]
    except IndexError as e:
        ppa_name = "ppa"
    sourceslistd = "/etc/apt/sources.list.d"
    line = "deb http://ppa.launchpad.net/%s/%s/ubuntu %s main" % (ppa_owner, ppa_name, distro_codename)
    filename = os.path.join(sourceslistd, "%s-%s-%s.list" % (encode(ppa_owner), encode(ppa_name), distro_codename))
    return (line, filename)

def expand_http_line(line, distro_codename):
    """
    short cut - this:
      apt-add-repository http://packages.medibuntu.org free non-free
    same as
      apt-add-repository 'deb http://packages.medibuntu.org/ base_codename free non-free'
    """
    if not line.startswith("http"):
      return line
    repo = line.split()[0]
    try:
        areas = line.split(" ",1)[1]
    except IndexError:
        areas = "main"
    line = "deb %s %s %s" % ( repo, distro_codename, areas )
    return line

class CurlCallback:
    def __init__(self):
        self.contents = ''

    def body_callback(self, buf):
        self.contents = self.contents + str(buf)


class PPAException(Exception):

    def __init__(self, value, original_error=None):
        self.value = value
        self.original_error = original_error

    def __str__(self):
        return repr(self.value)

SPEED_PIX_WIDTH = 125
SPEED_PIX_HEIGHT = 16

class Component():
    def __init__(self, name, description, selected):
        self.name = name
        self.description = description
        self.selected = selected
        self.widget = None

    def set_widget(self, widget):
        self.widget = widget

class Key():
    def __init__(self, pub):
        self.pub = pub
        self.sub = ""
        self.uid = ""

    def delete(self):
        os.system("apt-key del '%s'" % self.pub)

    def get_name(self):
        return "%s\n<small>    %s</small>" % (GObject.markup_escape_text(self.uid), GObject.markup_escape_text(self.pub))

class Mirror():
    def __init__(self, country_code, url, name):
        self.country_code = country_code
        self.url = url
        self.name = name

class Repository():
    def __init__(self, application, line, file, selected):
        self.application = application
        self.line = line
        self.file = file
        self.selected = selected

    def switch(self):
        self.selected = (not self.selected)

        readfile = open(self.file, "r")
        content = readfile.read()
        readfile.close()

        if self.selected:
            content = content.replace("#%s" % self.line, self.line)
            content = content.replace("# %s" % self.line, self.line)
        else:
            content = content.replace(self.line, "# %s" % self.line)

        with open(self.file, "w") as writefile:
            writefile.write(content)

        self.application.enable_reload_button()

    def edit(self, newline):
        readfile = open(self.file, "r")
        content = readfile.read()
        readfile.close()
        content = content.replace(self.line, newline)
        with open(self.file, "w") as writefile:
            writefile.write(content)
        self.line = newline
        self.application.enable_reload_button()

    def delete(self):
        readfile = open(self.file, "r")
        content = readfile.read()
        readfile.close()
        content = content.replace(self.line, "")
        with open(self.file, "w") as writefile:
            writefile.write(content)

        # If the file no longer contains any "deb" instances, delete it as well
        if "deb" not in content:
            os.unlink(self.file)

        self.application.enable_reload_button()

    def get_ppa_name(self):
        elements = self.line.split(" ")
        name = elements[1].replace("deb-src ", "")
        name = name.replace("deb ", "")
        name = name.replace("http://ppa.launchpad.net/", "")
        name = name.replace("/ubuntu", "")
        name = name.replace("/ppa", "")
        if self.line.startswith("deb-src"):
            suffix = _("Sources")
            name = "%s (%s)" % (name, suffix)
        return "<b>%s</b>\n<small><i>%s</i></small>\n<small><i>%s</i></small>" % (name, self.line, self.file)

    def get_repository_name(self):
        line = self.line.strip()
        name = line
        if line.startswith("deb cdrom:"):
            name = _("CD-ROM (Installation Disc)")
        else:
            try:
                elements = self.line.split(" ")
                for element in elements:
                   for protocol in ['http://', 'ftp://', 'https://']:
                        if element.startswith(protocol):
                            name = element.replace(protocol, "").split("/")[0]
                            subparts = name.split(".")
                            if len(subparts) > 2:
                                if subparts[-2] != "co":
                                    name = subparts[-2].capitalize()
                                else:
                                    name = subparts[-3].capitalize()
                            break
                name = name.replace("Linuxmint", "Linux Mint")
                name = name.replace("01", "Intel")
                name = name.replace("Steampowered", "Steam")
            except:
                pass
            if self.line.startswith("deb-src"):
                name = "%s (%s)" % (name, _("Sources"))
        return "<b>%s</b>\n<small><i>%s</i></small>\n<small><i>%s</i></small>" % (name, self.line, self.file)

class ComponentToggleCheckBox(Gtk.CheckButton):
    def __init__(self, application, component, window):
        self.application = application
        self.component = component
        self.window_object = window
        Gtk.CheckButton.__init__(self, self.component.description)
        self.set_active(component.selected)
        self.connect("toggled", self._on_toggled)

    def _on_toggled(self, widget):
        # As long as the interface isn't fully loaded, don't do anything
        if not self.application._interface_loaded:
            return

        if widget.get_active() and os.path.exists("/etc/linuxmint/info"):
            if self.component.name == "romeo":
                if self.application.show_confirmation_dialog(self.application._main_window, _("Linux Mint uses Romeo to publish packages which are not tested. Once these packages are tested, they are then moved to the official repositories. Unless you are participating in beta-testing, you should not enable this repository. Are you sure you want to enable Romeo?"), yes_no=True):
                    self.component.selected = widget.get_active()
                    self.application.apply_official_sources()
                else:
                    widget.set_active(not widget.get_active())
            else:
                self.component.selected = widget.get_active()
                self.application.apply_official_sources()
        else:
            self.component.selected = widget.get_active()
            self.application.apply_official_sources()

class MirrorSelectionDialog(object):
    MIRROR_COLUMN = 0
    MIRROR_URL_COLUMN = 1
    MIRROR_COUNTRY_FLAG_COLUMN = 2
    MIRROR_SPEED_COLUMN = 3
    MIRROR_SPEED_LABEL_COLUMN = 4
    MIRROR_TOOLTIP_COLUMN = 5
    MIRROR_NAME_COLUMN = 6

    def __init__(self, application, ui_builder):
        self._application = application
        self._ui_builder = ui_builder

        self._dialog = ui_builder.get_object("mirror_selection_dialog")
        self._dialog.set_transient_for(application._main_window)

        self._dialog.set_title(_("Select a mirror"))

        self._mirrors_model = Gtk.ListStore(object, str, GdkPixbuf.Pixbuf, float, str, str, str)
        # mirror, name, flag, speed, speed label, country code (used to sort by flag), mirror name
        self._treeview = ui_builder.get_object("mirrors_treeview")
        self._treeview.set_model(self._mirrors_model)
        self._treeview.set_headers_clickable(True)
        self._treeview.connect("row-activated", self._row_activated)

        self._mirrors_model.set_sort_column_id(MirrorSelectionDialog.MIRROR_SPEED_COLUMN, Gtk.SortType.DESCENDING)

        r = Gtk.CellRendererPixbuf()
        col = Gtk.TreeViewColumn(_("Country"), r, pixbuf = MirrorSelectionDialog.MIRROR_COUNTRY_FLAG_COLUMN)
        self._treeview.append_column(col)
        col.set_sort_column_id(MirrorSelectionDialog.MIRROR_TOOLTIP_COLUMN)

        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("URL"), r, text = MirrorSelectionDialog.MIRROR_NAME_COLUMN)
        self._treeview.append_column(col)
        col.set_sort_column_id(MirrorSelectionDialog.MIRROR_NAME_COLUMN)

        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("Speed"), r, text = MirrorSelectionDialog.MIRROR_SPEED_LABEL_COLUMN)
        self._treeview.append_column(col)
        col.set_sort_column_id(MirrorSelectionDialog.MIRROR_SPEED_COLUMN)
        col.set_min_width(int(1.1 * SPEED_PIX_WIDTH))

        self._treeview.set_tooltip_column(MirrorSelectionDialog.MIRROR_TOOLTIP_COLUMN)

        self.country_info = CountryInformation()

        with open('/usr/lib/linuxmint/mintSources/countries.json') as data_file:
            self.countries = json.load(data_file)

    def _row_activated(self, treeview, path, view_column):
        self._dialog.response(Gtk.ResponseType.APPLY)

    def get_country(self, country_code):
        for country in self.countries:
            if country["cca2"] == country_code:
                return country
        return None

    def _update_list(self):
        self._mirrors_model.clear()
        for mirror in self.visible_mirrors:
            if mirror.country_code == "WD":
                flag = FLAG_PATH % '_united_nations'
                country_name = _("Worldwide")
            else:
                flag = FLAG_PATH % mirror.country_code.lower()
                country_name = self.country_info.get_country_name(mirror.country_code)
            if not os.path.exists(flag):
                flag = FLAG_PATH % '_generic'
            tooltip = country_name
            if mirror.name != mirror.url:
                tooltip = "%s: %s" % (country_name, mirror.name)
            self._mirrors_model.append((
                mirror,
                mirror.url,
                GdkPixbuf.Pixbuf.new_from_file_at_size(flag, -1, FLAG_SIZE),
                0,
                None,
                tooltip,
                mirror.name
            ))

        self._all_speed_tests()

    def get_url_last_modified(self, url):
        try:
            c = pycurl.Curl()
            c.setopt(pycurl.URL, url)
            c.setopt(pycurl.CONNECTTIMEOUT, 5)
            c.setopt(pycurl.TIMEOUT, 30)
            c.setopt(pycurl.FOLLOWLOCATION, 1)
            c.setopt(pycurl.NOBODY, 1)
            c.setopt(pycurl.OPT_FILETIME, 1)
            c.perform()
            filetime = c.getinfo(pycurl.INFO_FILETIME)
            if filetime < 0:
                return None
            else:
                return filetime
        except:
            return None

    def check_mirror_up_to_date(self, url):
        if (self.default_mirror_age is None or self.default_mirror_age < 2):
            # If the default server was updated recently, the age is irrelevant (it would measure the time between now and the last update)
            return True
        mirror_timestamp = self.get_url_last_modified(url)
        if mirror_timestamp is None:
            print ("Error: Can't find the age of %s !!" % url)
            return False
        mirror_date = datetime.datetime.fromtimestamp(mirror_timestamp)
        mirror_age = (self.default_mirror_date - mirror_date).days
        if (mirror_age > 2):
            print ("Error: %s is out of date by %d days!" % (url, mirror_age))
            return False
        else:
            # Age is fine :)
            return True

    @async
    def _all_speed_tests(self):
        model_iters = [] # Don't iterate through iters directly.. we're modifying their orders..
        iter = self._mirrors_model.get_iter_first()
        while iter is not None:
            model_iters.append(iter)
            iter = self._mirrors_model.iter_next(iter)

        for iter in model_iters:
            try:
                if iter is not None:
                    mirror = self._mirrors_model.get_value(iter, MirrorSelectionDialog.MIRROR_COLUMN)
                    if mirror in self.visible_mirrors:
                        url = self._mirrors_model.get_value(iter, MirrorSelectionDialog.MIRROR_URL_COLUMN)
                        self._speed_test (iter, url)
            except Exception as e:
                pass # null types will occur here...

    def _get_speed_label(self, speed):
        if speed > 0:
            divider = (1024 * 1.0)
            represented_speed = (speed / divider)   # translate it to kB/S
            unit = _("kB/s")
            if represented_speed > divider:
                represented_speed = (represented_speed / divider)   # translate it to MB/S
                unit = _("MB/s")
            if represented_speed > divider:
                represented_speed = (represented_speed / divider)   # translate it to GB/S
                unit = _("GB/s")
            num_int_digits = len("%d" % represented_speed)
            if (num_int_digits > 2):
                represented_speed = "%d %s" % (represented_speed, unit)
            else:
                represented_speed = "%.1f %s" % (represented_speed, unit)
            represented_speed = represented_speed.replace(".0", "")
        else:
            represented_speed = ("0 %s") % _("kB/s")
        return represented_speed

    def _speed_test(self, iter, url):
        download_speed = 0
        try:
            if self.is_base:
                test_url = "%s/dists/%s/main/binary-amd64/Packages.gz" % (url, self.codename)
            else:
                test_url = "%s/dists/%s/main/Contents-amd64.gz" % (url, self.codename)
            if (self.is_base or self.check_mirror_up_to_date("%s/db/version" % url)):
                c = pycurl.Curl()
                buff = BytesIO()
                c.setopt(pycurl.URL, test_url)
                c.setopt(pycurl.CONNECTTIMEOUT, 5)
                c.setopt(pycurl.TIMEOUT, 20)
                c.setopt(pycurl.FOLLOWLOCATION, 1)
                c.setopt(pycurl.WRITEFUNCTION, buff.write)
                c.setopt(pycurl.NOSIGNAL, 1)
                c.perform()
                download_speed = c.getinfo(pycurl.SPEED_DOWNLOAD) # bytes/sec
            else:
                # the mirror is not up to date
                download_speed = -1
        except Exception as error:
            print ("Error '%s' on url %s" % (error, url))
            download_speed = 0

        self.show_speed_test_result(iter, download_speed)

    @idle
    def show_speed_test_result(self, iter, download_speed):
        if (iter is not None): # recheck as it can get null
            if download_speed == -1:
                # don't remove from model as this is not thread-safe
                self._mirrors_model.set_value(iter, MirrorSelectionDialog.MIRROR_SPEED_LABEL_COLUMN, _("Obsolete"))
            if download_speed == 0:
                # don't remove from model as this is not thread-safe
                self._mirrors_model.set_value(iter, MirrorSelectionDialog.MIRROR_SPEED_LABEL_COLUMN, _("Unreachable"))
            else:
                self._mirrors_model.set_value(iter, MirrorSelectionDialog.MIRROR_SPEED_COLUMN, download_speed)
                self._mirrors_model.set_value(iter, MirrorSelectionDialog.MIRROR_SPEED_LABEL_COLUMN, self._get_speed_label(download_speed))

    def run(self, mirrors, config, is_base):

        self.config = config
        self.is_base = is_base
        if self.is_base:
            self.codename = self.config["general"]["base_codename"]
            self.default_mirror = self.config["mirrors"]["base_default"]
        else:
            self.codename = self.config["general"]["codename"]
            self.default_mirror = self.config["mirrors"]["default"]

        # Try to find out where we're located...
        try:
            lookup = str(urlopen('http://geoip.ubuntu.com/lookup').read())
            cur_country_code = re.search('<CountryCode>(.*)</CountryCode>', lookup).group(1)
            if cur_country_code == 'None': cur_country_code = None
        except Exception as detail:
            cur_country_code = None  # no internet connection

        self.local_country_code = cur_country_code or os.environ.get('LANG', 'US').split('.')[0].split('_')[-1]  # fallback to LANG location or 'US'

        self.bordering_countries = []
        self.subregion = []
        self.region = []
        self.local_country = self.get_country(self.local_country_code)
        if self.local_country is not None:
            for country in self.countries:
                country_code = country["cca2"]
                if country["region"] == self.local_country["region"]:
                    if country["subregion"] == self.local_country["subregion"]:
                        self.subregion.append(country_code)
                    else:
                        self.region.append(country_code)
                if country["cca3"] in self.local_country["borders"]:
                    self.bordering_countries.append(country_code)

        self.worldwide_mirrors = []
        self.local_mirrors = []
        self.bordering_mirrors = []
        self.subregional_mirrors = []
        self.regional_mirrors = []
        self.official_mirrors = []
        self.other_mirrors = []

        for mirror in mirrors:
            if mirror.country_code == "WD":
                self.worldwide_mirrors.append(mirror)
                print (mirror)
            elif mirror.country_code == self.local_country_code:
                self.local_mirrors.append(mirror)
            elif mirror.country_code in self.bordering_countries:
                self.bordering_mirrors.append(mirror)
            elif mirror.country_code in self.subregion:
                self.subregional_mirrors.append(mirror)
            elif mirror.country_code in self.region:
                self.regional_mirrors.append(mirror)
            elif mirror.url == self.default_mirror:
                self.official_mirrors.append(mirror)
            else:
                self.other_mirrors.append(mirror)

        self.worldwide_mirrors = sorted(self.worldwide_mirrors, key=lambda x: x.country_code)
        self.bordering_mirrors = sorted(self.bordering_mirrors, key=lambda x: x.country_code)
        self.subregional_mirrors = sorted(self.subregional_mirrors, key=lambda x: x.country_code)
        self.regional_mirrors = sorted(self.regional_mirrors, key=lambda x: x.country_code)

        self.visible_mirrors = self.worldwide_mirrors + self.local_mirrors + self.bordering_mirrors + self.subregional_mirrors + self.regional_mirrors + self.official_mirrors

        if self.local_country_code in ["IL"]:
            # For some countries, geographical proximity doesn't equate to faster mirrors.
            self.visible_mirrors = self.visible_mirrors + self.other_mirrors

        if len(self.visible_mirrors) < 2:
            # We failed to identify the continent/country, let's show all mirrors
            self.visible_mirrors = mirrors

        # Try to find the age of the Mint archive
        self.default_mirror_age = None
        self.default_mirror_date = None
        mirror_timestamp = self.get_url_last_modified("%s/db/version" % self.default_mirror)
        if mirror_timestamp is not None:
            self.default_mirror_date = datetime.datetime.fromtimestamp(mirror_timestamp)
            now = datetime.datetime.now()
            self.default_mirror_age = (now - self.default_mirror_date).days

        self._update_list()
        self._dialog.show_all()
        retval = self._dialog.run()
        if retval == Gtk.ResponseType.APPLY:
            try:
                model, path = self._treeview.get_selection().get_selected_rows()
                iter = model.get_iter(path[0])
                res = model.get(iter, MirrorSelectionDialog.MIRROR_URL_COLUMN)[0]
            except:
                res = None
        else:
            res = None
        self._dialog.hide()
        self._mirrors_model.clear()
        return res

class Application(object):
    def __init__(self):

        # Prevent settings from being saved until the interface is fully loaded
        self._interface_loaded = False

        self.infobar_visible = False

        self.lsb_codename = subprocess.getoutput("lsb_release -sc")

        glade_file = "/usr/lib/linuxmint/mintSources/mintSources.glade"

        self.builder = Gtk.Builder()
        self.builder.set_translation_domain("mintsources")
        self.builder.add_from_file(glade_file)
        self._main_window = self.builder.get_object("main_window")

        self._main_window.set_title(_("Software Sources"))

        self._main_window.set_icon_name("software-sources")

        self._notebook = self.builder.get_object("notebook")
        self._official_repositories_box = self.builder.get_object("official_repositories_box")

        self.apt = mintcommon.APT(self._main_window)

        config_parser = configparser.RawConfigParser()
        config_parser.read("/usr/share/mintsources/%s/mintsources.conf" % self.lsb_codename)
        self.config = {}
        self.optional_components = []
        self.system_keys = []
        for section in config_parser.sections():
            if section.startswith("optional_component"):
                component_name = config_parser.get(section, "name")
                component_description = config_parser.get(section, "description")
                if component_name in ["backport", "backports"]:
                    component_description = "%s (%s)" % (_("Backported packages"), component_name)
                elif component_name in ["romeo", "unstable"]:
                    component_description = "%s (%s)" % (_("Unstable packages"), component_name)
                component = Component(component_name, component_description, False)
                self.optional_components.append(component)
            elif section.startswith("key"):
                self.system_keys.append(config_parser.get(section, "pub"))
            else:
                self.config[section] = {}
                for param in config_parser.options(section):
                    self.config[section][param] = config_parser.get(section, param)

        if self.config["general"]["use_ppas"] == "false":
            self.builder.get_object("vbuttonbox1").remove(self.builder.get_object("toggle_ppas"))

        self.builder.get_object("label_mirror_description").set_markup("%s (%s)" % (_("Main"), self.config["general"]["codename"]) )
        self.builder.get_object("label_base_mirror_description").set_markup("%s (%s)" % (_("Base"), self.config["general"]["base_codename"]) )
        self.builder.get_object("source_code_cb").connect("toggled", self.apply_official_sources)

        self.selected_components = []
        if (len(self.optional_components) > 0):
            if os.path.exists("/etc/linuxmint/info"):
                # This is Mint, we want to warn people about Romeo
                warning_label = Gtk.Label()
                #warning_label.set_alignment(0, 0.5)
                warning_label.set_markup("<span font_style='oblique' font_stretch='ultracondensed'>%s</span>" % _("Warning: Unstable packages can introduce regressions and negatively impact your system. Please do not enable these options in Linux Mint unless it was suggested by the development team."))
                warning_label.set_line_wrap(True)
                warning_label.set_justify(Gtk.Justification.FILL)
                self.builder.get_object("vbox_optional_components").pack_start(warning_label, True, True, 6)
            components_table = Gtk.Table()
            self.builder.get_object("vbox_optional_components").pack_start(components_table, True, True, 0)
            self.builder.get_object("vbox_optional_components").show_all()
            nb_components = 0
            for i in range(len(self.optional_components)):
                component = self.optional_components[i]
                cb = ComponentToggleCheckBox(self, component, self._main_window)
                component.set_widget(cb)
                components_table.attach(cb, 0, 1, nb_components, nb_components + 1)
                nb_components += 1

        self.mirrors = self.read_mirror_list(self.config["mirrors"]["mirrors"])
        self.base_mirrors = self.read_mirror_list(self.config["mirrors"]["base_mirrors"])

        self.repositories = []
        self.ppas = []

        source_files = []
        if os.path.exists("/etc/apt/sources.list"):
            source_files.append("/etc/apt/sources.list")
        for file in os.listdir("/etc/apt/sources.list.d"):
            if file.endswith(".list"):
                source_files.append("/etc/apt/sources.list.d/%s" % file)

        if "/etc/apt/sources.list.d/official-package-repositories.list" in source_files:
            source_files.remove("/etc/apt/sources.list.d/official-package-repositories.list")

        if "/etc/apt/sources.list.d/official-source-repositories.list" in source_files:
            source_files.remove("/etc/apt/sources.list.d/official-source-repositories.list")

        for source_file in source_files:
            file = open(source_file, "r")
            for line in file.readlines():
                line = line.strip()
                if line != "":
                    selected = True
                    if line.startswith("#"):
                        line = line.replace('#', '').strip()
                        selected = False
                    if line.startswith("deb"):
                        repository = Repository(self, line, source_file, selected)
                        if "ppa.launchpad" in line and self.config["general"]["use_ppas"] != "false":
                            self.ppas.append(repository)
                        else:
                            self.repositories.append(repository)
            file.close()

        # Add PPAs
        self._ppa_model = Gtk.ListStore(object, bool, str)
        self._ppa_treeview = self.builder.get_object("treeview_ppa")
        self._ppa_treeview.set_model(self._ppa_model)
        self._ppa_treeview.set_headers_clickable(True)
        self._ppa_treeview.connect("row-activated", self.on_ppa_treeview_doubleclick)
        selection = self._ppa_treeview.get_selection()
        selection.connect("changed", self.ppa_selected)

        self._ppa_model.set_sort_column_id(2, Gtk.SortType.ASCENDING)

        r = Gtk.CellRendererToggle()
        r.connect("toggled", self.ppa_toggled)
        col = Gtk.TreeViewColumn(_("Enabled"), r)
        col.set_cell_data_func(r, self.datafunction_checkbox)
        self._ppa_treeview.append_column(col)
        col.set_sort_column_id(1)

        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("PPA"), r, markup = 2)
        self._ppa_treeview.append_column(col)
        col.set_sort_column_id(2)

        if (len(self.ppas) > 0):
            for repository in self.ppas:
                tree_iter = self._ppa_model.append((repository, repository.selected, repository.get_ppa_name()))

        # Add repositories
        self._repository_model = Gtk.ListStore(object, bool, str)
        self._repository_treeview = self.builder.get_object("treeview_repository")
        self._repository_treeview.set_model(self._repository_model)
        self._repository_treeview.set_headers_clickable(True)

        self._repository_model.set_sort_column_id(2, Gtk.SortType.ASCENDING)

        r = Gtk.CellRendererToggle()
        r.connect("toggled", self.repository_toggled)
        col = Gtk.TreeViewColumn(_("Enabled"), r)
        col.set_cell_data_func(r, self.datafunction_checkbox)
        self._repository_treeview.append_column(col)
        col.set_sort_column_id(1)

        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("Repository"), r, markup = 2)
        self._repository_treeview.append_column(col)
        col.set_sort_column_id(2)

        if (len(self.repositories) > 0):
            for repository in self.repositories:
                tree_iter = self._repository_model.append((repository, repository.selected, repository.get_repository_name()))

        self._keys_model = Gtk.ListStore(object, str)
        self._keys_treeview = self.builder.get_object("treeview_keys")
        self._keys_treeview.set_model(self._keys_model)
        self._keys_treeview.set_headers_clickable(True)

        self._keys_model.set_sort_column_id(1, Gtk.SortType.ASCENDING)

        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(_("Key"), r, markup = 1)
        self._keys_treeview.append_column(col)
        col.set_sort_column_id(1)

        self.load_keys()

        if not os.path.exists("/etc/apt/sources.list.d/official-package-repositories.list"):
            print ("Sources missing, generating default sources list!")
            self.generate_missing_sources()

        self.detect_official_sources()

        self.builder.get_object("revert_button").connect("clicked", self.revert_to_default_sources)

        self._tab_buttons = [
            self.builder.get_object("toggle_official_repos"),
            self.builder.get_object("toggle_ppas"),
            self.builder.get_object("toggle_additional_repos"),
            self.builder.get_object("toggle_authentication_keys"),
            self.builder.get_object("toggle_maintenance")
        ]

        self._main_window.connect("delete_event", lambda w,e: Gtk.main_quit())
        for i in range(len(self._tab_buttons)):
            self._tab_buttons[i].connect("clicked", self._on_tab_button_clicked, i)
            self._tab_buttons[i].set_active(False)


        self.mirror_selection_dialog = MirrorSelectionDialog(self, self.builder)

        self.builder.get_object("button_mirror").connect("clicked", self.select_new_mirror)
        self.builder.get_object("button_base_mirror").connect("clicked", self.select_new_base_mirror)

        self.builder.get_object("button_ppa_add").connect("clicked", self.add_ppa)
        self.builder.get_object("button_ppa_edit").connect("clicked", self.edit_ppa)
        self.builder.get_object("button_ppa_remove").connect("clicked", self.remove_ppa)
        self.builder.get_object("button_ppa_examine").connect("clicked", self.examine_ppa)
        self.builder.get_object("button_ppa_examine").set_sensitive(False)

        self.builder.get_object("button_repository_add").connect("clicked", self.add_repository)
        self.builder.get_object("button_repository_edit").connect("clicked", self.edit_repository)
        self.builder.get_object("button_repository_remove").connect("clicked", self.remove_repository)

        self.builder.get_object("button_keys_add").connect("clicked", self.add_key)
        self.builder.get_object("button_keys_fetch").connect("clicked", self.fetch_key)
        self.builder.get_object("button_keys_remove").connect("clicked", self.remove_key)

        self.builder.get_object("button_mergelist").connect("clicked", self.fix_mergelist)
        self.builder.get_object("button_purge").connect("clicked", self.fix_purge)
        self.builder.get_object("button_remove_foreign").connect("clicked", self.remove_foreign)
        self.builder.get_object("button_downgrade_foreign").connect("clicked", self.downgrade_foreign)

        # From now on, we handle modifications to the settings and save them when they happen
        self._interface_loaded = True

    def set_button_text(self, label, text):
        label.set_text(text)
        if len(text) > BUTTON_LABEL_MAX_LENGTH:
            label.set_tooltip_text(text)
            label.set_max_width_chars(BUTTON_LABEL_MAX_LENGTH)
            label.set_ellipsize(Pango.EllipsizeMode.END)

    def read_mirror_list(self, path):
        mirror_list = []
        country_code = None
        mirrorsfile = open(path, "r")
        for line in mirrorsfile.readlines():
            line = line.strip()
            if line != "":
                if ("#LOC:" in line):
                    country_code = line.split(":")[1]
                else:
                    if country_code is not None:
                        if ("ubuntu-ports" not in line):
                            elements = line.split(" ")
                            url = elements[0]
                            if len(elements) > 1:
                                name = " ".join(elements[1:])
                            else:
                                name = url
                            if url[-1] == "/":
                                url = url[:-1]
                            mirror = Mirror(country_code, url, name)
                            mirror_list.append(mirror)
        return mirror_list

    def remove_foreign(self, widget):
        os.system("/usr/lib/linuxmint/mintSources/foreign_packages.py remove &")

    def downgrade_foreign(self, widget):
        os.system("/usr/lib/linuxmint/mintSources/foreign_packages.py downgrade &")

    def fix_purge(self, widget):
        os.system("aptitude purge ~c -y")
        image = Gtk.Image()
        image.set_from_icon_name("mintsources-maintenance", Gtk.IconSize.DIALOG)
        self.show_confirmation_dialog(self._main_window, _("There is no more residual configuration on the system."), image, affirmation=True)

    def fix_mergelist(self, widget):
        os.system("rm /var/lib/apt/lists/* -vf")
        image = Gtk.Image()
        image.set_from_icon_name("mintsources-maintenance", Gtk.IconSize.DIALOG)
        self.show_confirmation_dialog(self._main_window, _("The problem was fixed. Please reload the cache."), image, affirmation=True)
        self.enable_reload_button()

    def load_keys(self):
        self.keys = []
        key = None
        output = subprocess.getoutput("apt-key list")
        lines = []
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("/etc/apt"):
                continue
            if line.startswith("-----"):
                continue
            if line == "":
                continue
            lines.append(line)

        for key_data in "\n".join(lines).split("pub   "):
            key_data = key_data.split("\n")
            if len(key_data) > 3:
                extra = key_data[0]
                pub = key_data[1]
                pub_short = "".join(pub.split()[-2:])
                name = key_data[2]
                name = name.replace("uid ", "")
                if "]" in name:
                    name = name.split("]")[1].strip()
                key = Key(pub_short)
                key.uid = name
                if pub_short not in self.system_keys:
                    self.keys.append(key)

        self._keys_model.clear()
        for key in self.keys:
            tree_iter = self._keys_model.append((key, key.get_name()))

    def add_key(self, widget):
        dialog = Gtk.FileChooserDialog(_("Open.."),
                               None,
                               Gtk.FileChooserAction.OPEN,
                               (_("Cancel"), Gtk.ResponseType.CANCEL,
                                _("Open"), Gtk.ResponseType.OK))
        dialog.set_default_response(Gtk.ResponseType.OK)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            subprocess.call(["apt-key", "add", dialog.get_filename()])
            self.load_keys()
            self.enable_reload_button()
        dialog.destroy()

    def fetch_key(self, widget):
        image = Gtk.Image()
        image.set_from_icon_name("mintsources-keys", Gtk.IconSize.DIALOG)
        line = self.show_entry_dialog(self._main_window, _("Please enter the 8 characters of the public key you want to download from keyserver.ubuntu.com:"), "", image)
        if line is not None:
            res = os.system("apt-key adv --keyserver keyserver.ubuntu.com --recv-keys %s" % line)
            self.load_keys()
            self.enable_reload_button()

    def remove_key(self, widget):
        selection = self._keys_treeview.get_selection()
        (model, iter) = selection.get_selected()
        if (iter != None):
            key = model.get(iter, 0)[0]
            image = Gtk.Image()
            image.set_from_icon_name("mintsources-keys", Gtk.IconSize.DIALOG)
            if (self.show_confirmation_dialog(self._main_window, _("Are you sure you want to permanently remove this key?"), image, yes_no=True)):
                key.delete()
                self.load_keys()

    def add_ppa(self, widget):
        image = Gtk.Image()
        image.set_from_icon_name("mintsources-ppa", Gtk.IconSize.DIALOG)
        start_line = ""
        clipboard_text = self.get_clipboard_text("ppa")
        if clipboard_text != None:
            start_line = clipboard_text
        else:
            start_line = "ppa:username/ppa"

        line = self.show_entry_dialog(self._main_window, _("Please enter the name of the PPA you want to add:"), start_line, image)
        if line is not None:
            user, sep, ppa_name = line.split(":")[1].partition("/")
            ppa_name = ppa_name or "ppa"
            try:
                ppa_info = get_ppa_info_from_lp(user, ppa_name, self.config["general"]["base_codename"])
            except Exception as detail:
                self.show_error_dialog(self._main_window, _("Cannot add PPA: '%s'.") % detail)
                return

            image = Gtk.Image()
            image.set_from_icon_name("mintsources-ppa", Gtk.IconSize.DIALOG)
            info_text = "%s\n\n%s\n\n%s\n\n%s" % (line, self.format_string(ppa_info["displayname"]), self.format_string(ppa_info["description"]), str(ppa_info["web_link"]))
            if self.show_confirm_ppa_dialog(self._main_window, info_text):
                (deb_line, file) = expand_ppa_line(line.strip(), self.config["general"]["base_codename"])
                deb_line = expand_http_line(deb_line, self.config["general"]["base_codename"])
                debsrc_line = 'deb-src' + deb_line[3:]

                # Add the key
                short_key = ppa_info["signing_key_fingerprint"][-8:]
                os.system("apt-key adv --keyserver keyserver.ubuntu.com --recv-keys %s" % short_key)
                self.load_keys()

                # Add the PPA in sources.list.d
                with open(file, "w") as text_file:
                    text_file.write("%s\n" % deb_line)
                    text_file.write("%s\n" % "# "+debsrc_line)

                # Add the package line in the UI
                repository = Repository(self, deb_line, file, True)
                self.ppas.append(repository)
                tree_iter = self._ppa_model.append((repository, repository.selected, repository.get_ppa_name()))

                # Add the source line in the UI
                repository = Repository(self, debsrc_line, file, False)
                self.ppas.append(repository)
                tree_iter = self._ppa_model.append((repository, repository.selected, repository.get_ppa_name()))

                self.enable_reload_button()


    def format_string(self, text):
        if text is None:
            text = ""
        text = text.replace("<", "&lt;").replace(">", "&gt;")
        return text

    def edit_ppa(self, widget):
        selection = self._ppa_treeview.get_selection()
        (model, iter) = selection.get_selected()
        if (iter != None):
            repository = model.get(iter, 0)[0]
            url = self.show_entry_dialog(self._main_window, _("Edit the URL of the PPA"), repository.line)
            if url is not None:
                repository.edit(url)
                model.set_value(iter, 2, repository.get_ppa_name())

    def remove_ppa(self, widget):
        selection = self._ppa_treeview.get_selection()
        (model, iter) = selection.get_selected()
        if (iter != None):
            repository = model.get(iter, 0)[0]
            if (self.show_confirmation_dialog(self._main_window, _("Are you sure you want to permanently remove this PPA?"), yes_no=True)):
                model.remove(iter)
                repository.delete()
                self.ppas.remove(repository)

    def ppa_selected(self, selection):
        try:
            self.builder.get_object("button_ppa_examine").set_sensitive(False)
            (model, iter) = selection.get_selected()
            if (iter != None):
                repository = model.get_value(iter, 0)
                ppa_name = model.get_value(iter, 2)
                if repository.line.startswith("deb http://ppa.launchpad.net"):
                    self.builder.get_object("button_ppa_examine").set_sensitive(True)
        except Exception as detail:
            print (detail)

    def on_ppa_treeview_doubleclick(self, treeview, path, column):
        self.examine_ppa(None)

    def examine_ppa(self, widget):
        try:
            selection = self._ppa_treeview.get_selection()
            (model, iter) = selection.get_selected()
            if (iter != None):
                repository = model.get_value(iter, 0)
                ppa_name = model.get_value(iter, 2)
                if repository.line.startswith("deb http://ppa.launchpad.net"):
                    line = repository.line.split()[1].replace("http://ppa.launchpad.net/", "")
                    if line.endswith("/ubuntu"):
                        line = line[:-7]
                        ppa_owner, ppa_name = line.split("/")
                        architecture = subprocess.getoutput("dpkg --print-architecture")
                        ppa_file = "/var/lib/apt/lists/ppa.launchpad.net_%s_%s_ubuntu_dists_%s_main_binary-%s_Packages" % (ppa_owner, ppa_name, self.config["general"]["base_codename"], architecture)
                        if os.path.exists(ppa_file):
                            os.system("/usr/lib/linuxmint/mintSources/ppa_browser.py %s %s %s &" % (self.config["general"]["base_codename"], ppa_owner, ppa_name))
                        else:
                            print ("%s not found!" % ppa_file)
                            self.show_error_dialog(self._main_window, _("The content of this PPA is not available. Please refresh the cache and try again."))
        except Exception as detail:
            print (detail)

    def add_repository(self, widget):
        image = Gtk.Image()
        image.set_from_icon_name("mintsources-additional", Gtk.IconSize.DIALOG)
        start_line = ""
        clipboard_text = self.get_clipboard_text("deb")
        if clipboard_text != None:
            start_line = clipboard_text
        else:
            start_line = "deb http://packages.domain.com/ %s main" % self.config["general"]["base_codename"]

        line = self.show_entry_dialog(self._main_window, _("Please enter the name of the repository you want to add:"), start_line, image)
        if line is not None and line.strip().startswith("deb"):
            # Add the repository in sources.list.d
            with open("/etc/apt/sources.list.d/additional-repositories.list", "a") as text_file:
                text_file.write("%s\n" % line)

            # Add the line in the UI
            repository = Repository(self, line, "/etc/apt/sources.list.d/additional-repositories.list", True)
            self.repositories.append(repository)
            tree_iter = self._repository_model.append((repository, repository.selected, repository.get_repository_name()))

            self.enable_reload_button()


    def edit_repository(self, widget):
        selection = self._repository_treeview.get_selection()
        (model, iter) = selection.get_selected()
        if (iter != None):
            repository = model.get(iter, 0)[0]
            url = self.show_entry_dialog(self._main_window, _("Edit the URL of the repository"), repository.line)
            if url is not None:
                repository.edit(url)
                model.set_value(iter, 2, repository.get_repository_name())

    def remove_repository(self, widget):
        selection = self._repository_treeview.get_selection()
        (model, iter) = selection.get_selected()
        if (iter != None):
            repository = model.get(iter, 0)[0]
            if (self.show_confirmation_dialog(self._main_window, _("Are you sure you want to permanently remove this repository?"), yes_no=True)):
                model.remove(iter)
                repository.delete()
                self.repositories.remove(repository)


    def show_confirmation_dialog(self, parent, message, image=None, affirmation=None, yes_no=False):
        buttons = Gtk.ButtonsType.OK_CANCEL
        default_button = Gtk.ResponseType.OK
        confirmation_button = Gtk.ResponseType.OK
        if yes_no:
            buttons = Gtk.ButtonsType.YES_NO
            default_button = Gtk.ResponseType.NO
            confirmation_button = Gtk.ResponseType.YES

        if affirmation is None:
            d = Gtk.MessageDialog(parent,
                              Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                              Gtk.MessageType.WARNING,
                              buttons,
                              message)
        else:
            d = Gtk.MessageDialog(parent,
                              Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                              Gtk.MessageType.INFO,
                              Gtk.ButtonsType.OK,
                              message)
        d.set_markup(message)
        if image is not None:
            image.show()
            d.set_image(image)

        d.set_default_response(default_button)
        r = d.run()
        d.destroy()
        if r == confirmation_button:
            return True
        else:
            return False

    def show_confirm_ppa_dialog(self, parent, message):
        b = Gtk.TextBuffer()
        b.set_text(message)
        t =  Gtk.TextView()
        t.set_buffer(b)
        t.set_wrap_mode(Pango.WrapMode.WORD)
        s = Gtk.ScrolledWindow()
        s.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        s.set_shadow_type(Gtk.ShadowType.OUT)
        default_button = Gtk.ResponseType.ACCEPT
        confirmation_button = Gtk.ResponseType.ACCEPT
        d = Gtk.Dialog(None, parent,
                       Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                       (_("Cancel"), Gtk.ResponseType.REJECT,
                       _("OK"), Gtk.ResponseType.ACCEPT))
        d.set_size_request(550, 400)
        d.vbox.pack_start(s, True, True, 0)
        d.set_title("")
        s.show()
        s.add(t)
        t.show()
        d.set_default_response(default_button)
        r = d.run()
        d.destroy()
        if r == confirmation_button:
            return True
        else:
            return False

    def show_error_dialog(self, parent, message, image=None):
        d = Gtk.MessageDialog(parent,
                              Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                              Gtk.MessageType.ERROR,
                              Gtk.ButtonsType.OK,
                              message)

        d.set_markup(message)
        if image is not None:
            image.show()
            d.set_image(image)

        d.set_default_response(Gtk.ResponseType.OK)
        r = d.run()
        d.destroy()
        if r == Gtk.ResponseType.OK:
            return True
        else:
            return False

    def show_entry_dialog(self, parent, message, default='', image=None):
        d = Gtk.MessageDialog(parent,
                              Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                              Gtk.MessageType.QUESTION,
                              Gtk.ButtonsType.OK_CANCEL,
                              message)

        d.set_markup(message)
        if image is not None:
            image.show()
            d.set_image(image)

        entry = Gtk.Entry()
        entry.set_text(default)
        entry.set_margin_start(6)
        entry.set_margin_end(6)
        entry.show()
        d.vbox.pack_end(entry, False, False, 0)
        entry.connect('activate', lambda _: d.response(Gtk.ResponseType.OK))
        d.set_default_response(Gtk.ResponseType.OK)

        r = d.run()
        text = entry.get_text()
        d.destroy()
        if r == Gtk.ResponseType.OK:
            return text
        else:
            return None

    def datafunction_checkbox(self, column, cell, model, iter, data):
        cell.set_property("activatable", True)
        if (model.get_value(iter, 0).selected):
            cell.set_property("active", True)
        else:
            cell.set_property("active", False)

    def ppa_toggled(self, renderer, path):
        iter = self._ppa_model.get_iter(path)
        if (iter != None):
            repository = self._ppa_model.get_value(iter, 0)
            repository.switch()

    def repository_toggled(self, renderer, path):
        iter = self._repository_model.get_iter(path)
        if (iter != None):
            repository = self._repository_model.get_value(iter, 0)
            repository.switch()

    def select_new_mirror(self, widget):
        url = self.mirror_selection_dialog.run(self.mirrors, self.config, False)
        if url is not None:
            self.selected_mirror = url
            self.builder.get_object("label_mirror_name").set_text(self.selected_mirror)
        self.apply_official_sources()

    def select_new_base_mirror(self, widget):
        url = self.mirror_selection_dialog.run(self.base_mirrors, self.config, True)
        if url is not None:
            self.selected_base_mirror = url
            self.builder.get_object("label_base_mirror_name").set_text(self.selected_base_mirror)
        self.apply_official_sources()

    def _on_tab_button_clicked(self, button, page_index):
        if page_index == self._notebook.get_current_page() and button.get_active() == True:
            return
        if page_index != self._notebook.get_current_page() and button.get_active() == False:
            return
        self._notebook.set_current_page(page_index)
        for i in self._tab_buttons:
            i.set_active(False)
        button.set_active(True)

    def run(self):
        self._main_window.show_all()
        Gtk.main()

    def revert_to_default_sources(self, widget):
        self.selected_mirror = self.config["mirrors"]["default"]
        self.builder.get_object("label_mirror_name").set_text(self.selected_mirror)
        self.selected_base_mirror = self.config["mirrors"]["base_default"]
        self.builder.get_object("label_base_mirror_name").set_text(self.selected_base_mirror)
        self.builder.get_object("source_code_cb").set_active(False)

        for component in self.optional_components:
            component.selected = False
            component.widget.set_active(False)

        self.apply_official_sources()

    def enable_reload_button(self):
        if not self.infobar_visible:
            self.infobar_visible = True
            infobar = Gtk.InfoBar()
            infobar.set_message_type(Gtk.MessageType.INFO)
            info_label = Gtk.Label()
            infobar_message = "%s\n<small>%s</small>" % (_("Your configuration changed"), _("Click OK to update your APT cache"))
            info_label.set_markup(infobar_message)
            infobar.get_content_area().pack_start(info_label,False, False,0)
            infobar.add_button(_("OK"), Gtk.ResponseType.OK)
            infobar.connect("response", self._on_infobar_response)
            self.builder.get_object("box_infobar").pack_start(infobar, True, True,0)
            infobar.show_all()

    def _on_infobar_response(self, infobar, response_id):
        infobar.destroy()
        self.infobar_visible = False
        self.apt.update_cache()

    def apply_official_sources(self, widget=None):
        # As long as the interface isn't fully loaded, don't save anything
        if not self._interface_loaded:
            return

        self.update_flags()

        # Check which components are selected
        selected_components = []
        for component in self.optional_components:
            if component.selected:
                selected_components.append(component.name)

        # Update official packages repositories
        os.system("rm -f /etc/apt/sources.list.d/official-package-repositories.list")
        template = open('/usr/share/mintsources/%s/official-package-repositories.list' % self.lsb_codename, 'r').read()
        template = template.replace("$codename", self.config["general"]["codename"])
        template = template.replace("$basecodename", self.config["general"]["base_codename"])
        template = template.replace("$optionalcomponents", ' '.join(selected_components))
        template = template.replace("$mirror", self.selected_mirror)
        template = template.replace("$basemirror", self.selected_base_mirror)

        with open("/etc/apt/sources.list.d/official-package-repositories.list", "w") as text_file:
            text_file.write(template)

        # Update official sources repositories
        os.system("rm -f /etc/apt/sources.list.d/official-source-repositories.list")
        if (self.builder.get_object("source_code_cb").get_active()):
            template = open('/usr/share/mintsources/%s/official-source-repositories.list' % self.lsb_codename, 'r').read()
            template = template.replace("$codename", self.config["general"]["codename"])
            template = template.replace("$basecodename", self.config["general"]["base_codename"])
            template = template.replace("$optionalcomponents", ' '.join(selected_components))
            template = template.replace("$mirror", self.selected_mirror)
            template = template.replace("$basemirror", self.selected_base_mirror)
            with open("/etc/apt/sources.list.d/official-source-repositories.list", "w") as text_file:
                text_file.write(template)

        self.enable_reload_button()

    def generate_missing_sources(self):
        os.system("rm -f /etc/apt/sources.list.d/official-package-repositories.list")
        os.system("rm -f /etc/apt/sources.list.d/official-source-repositories.list")

        template = open('/usr/share/mintsources/%s/official-package-repositories.list' % self.lsb_codename, 'r').read()
        template = template.replace("$codename", self.config["general"]["codename"])
        template = template.replace("$basecodename", self.config["general"]["base_codename"])
        template = template.replace("$optionalcomponents", '')
        template = template.replace("$mirror", self.config["mirrors"]["default"])
        template = template.replace("$basemirror", self.config["mirrors"]["base_default"])

        with open("/etc/apt/sources.list.d/official-package-repositories.list", "w") as text_file:
            text_file.write(template)

    def detect_official_sources(self):
        self.selected_mirror = self.config["mirrors"]["default"]
        self.selected_base_mirror = self.config["mirrors"]["base_default"]

        # Detect source code repositories
        self.builder.get_object("source_code_cb").set_active(os.path.exists("/etc/apt/sources.list.d/official-source-repositories.list"))

        listfile = open('/etc/apt/sources.list.d/official-package-repositories.list', 'r')
        for line in listfile.readlines():
            if (self.config["detection"]["main_identifier"] in line):
                for component in self.optional_components:
                    if component.name in line:
                        component.widget.set_active(True)
                elements = line.split(" ")
                if elements[0] == "deb":
                    mirror = elements[1]
                    if "$" not in mirror:
                        self.selected_mirror = mirror.rstrip('/')
            if (self.config["detection"]["base_identifier"] in line):
                elements = line.split(" ")
                if elements[0] == "deb":
                    mirror = elements[1]
                    if "$" not in mirror:
                        self.selected_base_mirror = mirror.rstrip('/')

        self.builder.get_object("label_mirror_name").set_text(self.selected_mirror)
        self.builder.get_object("label_base_mirror_name").set_text(self.selected_base_mirror)

        self.update_flags()

    def update_flags(self):
        mint_flag_path = FLAG_PATH % '_generic'
        base_flag_path = FLAG_PATH % '_generic'

        selected_mirror = self.selected_mirror
        if selected_mirror[-1] == "/":
            selected_mirror = selected_mirror[:-1]

        selected_base_mirror = self.selected_base_mirror
        if selected_base_mirror[-1] == "/":
            selected_base_mirror = selected_base_mirror[:-1]

        for mirror in self.mirrors:
            if mirror.url[-1] == "/":
                url = mirror.url[:-1]
            else:
                url = mirror.url
            if url in selected_mirror:
                if mirror.country_code == "WD":
                    flag = FLAG_PATH % '_united_nations'
                else:
                    flag = FLAG_PATH % mirror.country_code.lower()
                if os.path.exists(flag):
                    mint_flag_path = flag
                    break

        for mirror in self.base_mirrors:
            if mirror.url[-1] == "/":
                url = mirror.url[:-1]
            else:
                url = mirror.url
            if url in selected_base_mirror:
                if os.path.exists(FLAG_PATH % mirror.country_code.lower()):
                    base_flag_path = FLAG_PATH % mirror.country_code.lower()
                    break

        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(mint_flag_path, -1, FLAG_SIZE)
        self.builder.get_object("image_mirror").set_from_pixbuf(pixbuf)

        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(base_flag_path, -1, FLAG_SIZE)
        self.builder.get_object("image_base_mirror").set_from_pixbuf(pixbuf)

    def get_clipboard_text(self, source_type):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        text = clipboard.wait_for_text()
        if text is not None and text.strip().startswith(source_type):
            return text
        else:
            return None

if __name__ == "__main__":
    usage = "usage: %prog [options] [repository]"
    parser = OptionParser(usage=usage)
    #add a dummy option which can be easily ignored
    parser.add_option("-?", dest="ignore", action="store_true", default=False)
    parser.add_option("-y", "--yes", dest="forceYes", action="store_true",
        help="force yes on all confirmation questions", default=False)
    parser.add_option("-r", "--remove", dest="remove", action="store_true",
        help="Remove the specified repository", default=False)

    (options, args) = parser.parse_args()

    lsb_codename = subprocess.getoutput("lsb_release -sc")
    config_dir = "/usr/share/mintsources/%s" % lsb_codename
    if not os.path.exists(config_dir):
        print ("LSB codename: '%s'." % lsb_codename)
        if os.path.exists("/etc/linuxmint/info"):
            print ("Version of base-files: '%s'." % subprocess.getoutput("dpkg-query -f '${Version}' -W base-files"))
            print ("Your LSB codename isn't a valid Linux Mint codename.")
        else:
            print ("This codename isn't currently supported.")
        print ("Please check your LSB information with \"lsb_release -a\".")
        sys.exit(1)

    if len(args) > 1 and (args[0] == "add-apt-repository"):
        ppa_line = args[1]
        lsb_codename = subprocess.getoutput("lsb_release -sc")
        config_parser = configparser.RawConfigParser()
        config_parser.read("/usr/share/mintsources/%s/mintsources.conf" % lsb_codename)
        codename = config_parser.get("general", "base_codename")
        use_ppas = config_parser.get("general", "use_ppas")
        if options.remove:
            remove_repository_via_cli(ppa_line, codename, options.forceYes)
        else:
            add_repository_via_cli(ppa_line, codename, options.forceYes, use_ppas)
    else:
        Application().run()
