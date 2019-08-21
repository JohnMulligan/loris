Install on Red Hat (CentOS) 7
=============================

Everything here needs to be done as root, so either open a root shell now or
precede all instructions here with `sudo`.
You should also, if you are running with SELinux, begin this process by turning it off (```setenforce=0```) and end by turning it back on (```setenforce=1```). Doing this, as with sudo, makes installation instructions tricky -- it can change access between different parts of the system in unexpected ways.

Install Dependencies
--------------------

 * mod_wsgi for apache
 * python-devel for `pip install Pillow`
 * bzip2 for library installed via Loris' `python setup.py install`

```
yum install git wget mod_wsgi python-devel bzip2 gcc
```

Install pip
-----------

At this point, you should have a `python2.7` binary in your `$PATH` somewhere
(probably at `/usr/local/bin/python2.7`). We need to install pip so we can get
the necessary dependencies for Loris. That's pretty simple: follow the
instructions on the [pip wesite](http://www.pip-installer.org/en/latest/installing.html).
Make sure you run the script with the correct Python or pip will install
packages to the wrong place.

```
cd /opt
wget https://bootstrap.pypa.io/get-pip.py
python get-pip.py
```

Install image libraries
-----------------------

Next you'll need to install all the necessary image libraries so that loris
will work properly. You can get what you need through yum:

```
yum install libjpeg-turbo libjpeg-turbo-devel \
    freetype freetype-devel \
    zlib-devel \
    libtiff-devel
```


Install pip dependencies
------------------------

```
pip install Werkzeug
pip install Pillow
```

Install Loris
-------------

```
mkdir /var/www/loris2
useradd -d /var/www/loris2 -s /sbin/false loris
git clone https://github.com/loris-imageserver/loris.git
cd loris
pip install -r requirements.txt --ignore-installed

# (configure as necessary in /opt/loris/etc/loris.conf)

python setup.py install
```

Caching & logging folders
-----------------------

The install script does not seem to create these folders which the default config file takes as necessities for logging and caching -- remember to give ownership to Loris.

```
mkdir /var/log/loris2
chown loris /var/log/loris2
mkdir /var/cache/loris
chown loris /var/cache/loris
```

Configure Apache
-------------

The Apache instructions here do **not** work well with CentOS7 SELinux boxes: [configuring Apache](apache.md).


1. Create a file loris.conf in /etc/httpd/conf.d/
```
    <virtualhost *:80> 
        AllowEncodedSlashes On
        ServerName [SERVER IP HERE]

        WSGIDaemonProcess loris user=loris group=loris processes=10 threads=15 maximum-requests=10000
        WSGIScriptAlias /loris /var/www/loris2/loris.wsgi
        <directory /var/www/loris2> 
            WSGIProcessGroup loris 
            WSGIApplicationGroup %{GLOBAL} 
            WSGIScriptReloading On 
        Require all granted 
        </directory> 
    </virtualhost>
 ```
 
 2. Create a file loris.wsgi in /var/www/loris2/
 ```
#!/usr/bin/env python

from loris.webapp import create_app
application = create_app(config_file_path='/opt/loris/etc/loris2.conf')
```

After apache is configured, start it up:
`httpd -k start`

You can test by adding an image to `/usr/local/share/images` and visit a URL like:
`http://{YOUR SERVER NAME}/loris/{YOUR TEST FILE NAME}/full/full/0/default.jpg`


SELinux module
---------------

If SELinux is enabled you will need to create a custom security module that you load into Red Hat to
allow httpd permissions to write to cache. You'll want to copy this into a
file called `loris.te` (at any rate, make sure the file name matches the
module name in the first line).

```
module loris 1.0;

require {
        type httpd_t;
        type var_t;
        class file { write read getattr open };
}

#============= httpd_t ==============
allow httpd_t var_t:file { write read getattr open };
```

Then, you'll need to create a `.mod` file and compile it into the policy module
itself a `.pp` file).

```
checkmodule -M -m -o loris.mod loris.te     # create mod file
semodule_package -m loris.mod -o loris.pp   # compile it
semodule -i loris.pp                              # install it
```

If all goes well, everything *should* be working properly!
