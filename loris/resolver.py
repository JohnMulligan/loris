# -*- coding: utf-8 -*-
"""
`resolver` -- Resolve Identifiers to Image Paths
================================================
"""
import errno
from logging import getLogger
from loris_exception import ResolverException
from os.path import join, exists, dirname
from os import makedirs, rename, remove
from shutil import copy
import tempfile
from urllib import unquote, quote_plus
from contextlib import closing

import constants
import hashlib
import glob
import requests
import re

logger = getLogger(__name__)


class _AbstractResolver(object):

    def __init__(self, config):
        self.config = config

    def is_resolvable(self, ident):
        """
        The idea here is that in some scenarios it may be cheaper to check
        that an id is resolvable than to actually resolve it. For example, for
        an HTTP resolver, this could be a HEAD instead of a GET.

        Args:
            ident (str):
                The identifier for the image.
        Returns:
            bool
        """
        cn = self.__class__.__name__
        raise NotImplementedError('is_resolvable() not implemented for %s' % (cn,))

    def resolve(self, ident):
        """
        Given the identifier of an image, get the path (fp) and format (one of.
        'jpg', 'tif', or 'jp2'). This will likely need to be reimplemented for
        environments and can be as smart or dumb as you want.

        Args:
            ident (str):
                The identifier for the image.
        Returns:
            (str, str): (fp, format)
        Raises:
            ResolverException when something goes wrong...
        """
        cn = self.__class__.__name__
        raise NotImplementedError('resolve() not implemented for %s' % (cn,))

    def format_from_ident(self, ident):
        if ident.rfind('.') != -1:
            extension = ident.split('.')[-1]
            if len(extension) < 5:
                extension = extension.lower()
                return constants.EXTENSION_MAP.get(extension, extension)
        message = 'Format could not be determined for: %s.' % (ident)
        raise ResolverException(404, message)


class SimpleFSResolver(_AbstractResolver):
    """
    For this dumb version a constant path is prepended to the identfier
    supplied to get the path It assumes this 'identifier' ends with a file
    extension from which the format is then derived.
    """

    def __init__(self, config):
        super(SimpleFSResolver, self).__init__(config)
        if 'src_img_roots' in self.config:
            self.source_roots = self.config['src_img_roots']
        else:
            self.source_roots = [self.config['src_img_root']]

    def raise_404_for_ident(self, ident):
        message = 'Source image not found for identifier: %s.' % (ident,)
        logger.warn(message)
        raise ResolverException(404, message)

    def source_file_path(self, ident):
        ident = unquote(ident)
        for directory in self.source_roots:
            fp = join(directory, ident)
            if exists(fp):
                return fp

    def is_resolvable(self, ident):
        return not self.source_file_path(ident) is None

    def resolve(self, ident):

        if not self.is_resolvable(ident):
            self.raise_404_for_ident(ident)

        source_fp = self.source_file_path(ident)
        logger.debug('src image: %s' % (source_fp,))

        format_ = self.format_from_ident(ident)

        return (source_fp, format_)


class ExtensionNormalizingFSResolver(SimpleFSResolver):
    '''This Resolver is deprecated - when resolving the identifier to an image
    format, all resolvers now automatically normalize (lower-case) file
    extensions and map 4-letter .tiff & .jpeg extensions to the 3-letter tif
    & jpg image formats Loris uses.
    '''
    pass


class SimpleHTTPResolver(_AbstractResolver):
    '''
    Example resolver that one might use if image files were coming from
    an http image store (like Fedora Commons). The first call to `resolve()`
    copies the source image into a local cache; subsequent calls use local
    copy from the cache.

    The config dictionary MUST contain
     * `cache_root`, which is the absolute path to the directory where source images
        should be cached.

    The config dictionary MAY contain
     * `source_prefix`, the url up to the identifier.
     * `source_suffix`, the url after the identifier (if applicable).
     * `default_format`, the format of images (will use content-type of response if not specified).
     * `head_resolvable` with value True, whether to make HEAD requests to verify object existence (don't set if using
        Fedora Commons prior to 3.8).
     * `uri_resolvable` with value True, allows one to use full uri's to resolve to an image.
     * `user`, the username to make the HTTP request as.
     * `pw`, the password to make the HTTP request as.
     * `ssl_check`, whether to check the validity of the origin server's HTTPS
     certificate. Set to False if you are using an origin server with a
     self-signed certificate.
     * `cert`, path to an SSL client certificate to use for authentication. If `cert` and `key` are both present, they take precedence over `user` and `pw` for authetication.
     * `key`, path to an SSL client key to use for authentication.
    '''
    def __init__(self, config):
        super(SimpleHTTPResolver, self).__init__(config)

        self.source_prefix = self.config.get('source_prefix', '')

        self.source_suffix = self.config.get('source_suffix', '')

        self.default_format = self.config.get('default_format', None)

        self.head_resolvable = self.config.get('head_resolvable', False)

        self.uri_resolvable = self.config.get('uri_resolvable', False)

        self.user = self.config.get('user', None)

        self.pw = self.config.get('pw', None)

        self.cert = self.config.get('cert', None)

        self.key = self.config.get('key', None)

        self.ssl_check = self.config.get('ssl_check', True)

        self.ident_regex = self.config.get('ident_regex', False)

        if 'cache_root' in self.config:
            self.cache_root = self.config['cache_root']
        else:
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Missing setting for cache_root.'
            logger.error(message)
            raise ResolverException(500, message)

        if not self.uri_resolvable and self.source_prefix == '':
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Must either set uri_resolvable' \
                      ' or source_prefix settings.'
            logger.error(message)
            raise ResolverException(500, message)

    def request_options(self):
        # parameters to pass to all head and get requests;
        options = {}
        if self.cert is not None and self.key is not None:
            options['cert'] = (self.cert, self.key)
        if self.user is not None and self.pw is not None:
            options['auth'] = (self.user, self.pw)
        options['verify'] = self.ssl_check
        return options

    def is_resolvable(self, ident):
        ident = unquote(ident)

        if self.ident_regex:
            regex = re.compile(self.ident_regex)
            if not regex.match(ident):
                return False

        fp = join(self.cache_root, SimpleHTTPResolver._cache_subroot(ident))
        if exists(fp):
            return True
        else:
            (url, options) = self._web_request_url(ident)

            if self.head_resolvable:
                response = requests.head(url, **options)
                if response.ok:
                    return True

            else:
                with closing(requests.get(url, stream=True, **options)) as response:
                    if response.ok:
                        return True

        return False

    def get_format(self, ident, potential_format):
        if self.default_format is not None:
            return self.default_format
        elif potential_format is not None:
            return potential_format
        else:
            return self.format_from_ident(ident)

    def _web_request_url(self, ident):
        if (ident.startswith('http://') or ident.startswith('https://')) and self.uri_resolvable:
            url = ident
        else:
            url = self.source_prefix + ident + self.source_suffix
        if not (url.startswith('http://') or url.startswith('https://')):
            logger.warn(
                'Bad URL request at %s for identifier: %s.' % (source_url, ident)
            )
            public_message = 'Bad URL request made for identifier: %s.' % (ident,)
            raise ResolverException(404, public_message)
        return (url, self.request_options())

    # Get a subdirectory structure for the cache_subroot through hashing.
    @staticmethod
    def _cache_subroot(ident):
        cache_subroot = ''

        # Split out potential pidspaces... Fedora Commons most likely use case.
        if ident[0:6] != 'http:/' and ident[0:7] != 'https:/' and len(ident.split(':')) > 1:
            for split_ident in ident.split(':')[0:-1]:
                cache_subroot = join(cache_subroot, split_ident)
        elif ident[0:6] == 'http:/' or ident[0:7] == 'https:/':
            cache_subroot = 'http'

        cache_subroot = join(cache_subroot, SimpleHTTPResolver._ident_file_structure(ident))

        return cache_subroot

    # Get the directory structure of the identifier itself
    @staticmethod
    def _ident_file_structure(ident):
        file_structure = ''
        ident_hash = hashlib.md5(quote_plus(ident)).hexdigest()
        # First level 2 digit directory then do three digits...
        file_structure_list = [ident_hash[0:2]] + [ident_hash[i:i+3] for i in range(2, len(ident_hash), 3)]

        for piece in file_structure_list:
            file_structure = join(file_structure, piece)

        return file_structure

    def cache_dir_path(self, ident):
        ident = unquote(ident)
        return join(
                self.cache_root,
                SimpleHTTPResolver._cache_subroot(ident)
        )

    def raise_404_for_ident(self, ident):
        message = 'Image not found for identifier: %s.' % (ident)
        raise ResolverException(404, message)

    def cached_file_for_ident(self, ident):
        cache_dir = self.cache_dir_path(ident)
        if exists(cache_dir):
            files = glob.glob(join(cache_dir, 'loris_cache.*'))
            if files:
                return files[0]
        return None

    def cache_file_extension(self, ident, response):
        if 'content-type' in response.headers:
            try:
                extension = self.get_format(ident, constants.FORMATS_BY_MEDIA_TYPE[response.headers['content-type']])
            except KeyError:
                logger.warn('Your server may be responding with incorrect content-types. Reported %s for ident %s.'
                            % (response.headers['content-type'], ident))
                # Attempt without the content-type
                extension = self.get_format(ident, None)
        else:
            extension = self.get_format(ident, None)
        return extension

    def _create_cache_dir(self, cache_dir):
        try:
            makedirs(cache_dir)
        except OSError as ose:
            if ose.errno == errno.EEXIST:
                pass
            else:
                raise

    def copy_to_cache(self, ident):
        ident = unquote(ident)
        cache_dir = self.cache_dir_path(ident)
        self._create_cache_dir(cache_dir)

        #get source image and write to temporary file
        (source_url, options) = self._web_request_url(ident)
        with closing(requests.get(source_url, stream=True, **options)) as response:
            if not response.ok:
                public_message = 'Source image not found for identifier: %s. Status code returned: %s' % (ident,response.status_code)
                log_message = 'Source image not found at %s for identifier: %s. Status code returned: %s' % (source_url,ident,response.status_code)
                logger.warn(log_message)
                raise ResolverException(404, public_message)

            extension = self.cache_file_extension(ident, response)
            local_fp = join(cache_dir, "loris_cache." + extension)

            with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as tmp_file:
                for chunk in response.iter_content(2048):
                    tmp_file.write(chunk)
                tmp_file.flush()

            #now rename the tmp file to the desired file name if it still doesn't exist
            #   (another process could have created it)
            if exists(local_fp):
                logger.info('another process downloaded src image %s' % local_fp)
                remove(tmp_file.name)
            else:
                rename(tmp_file.name, local_fp)
                logger.info("Copied %s to %s" % (source_url, local_fp))

        return local_fp

    def resolve(self, ident):
        cached_file_path = self.cached_file_for_ident(ident)
        if not cached_file_path:
            cached_file_path = self.copy_to_cache(ident)
        format_ = self.get_format(cached_file_path, None)
        return (cached_file_path, format_)


class TemplateHTTPResolver(SimpleHTTPResolver):
    '''HTTP resolver that suppors multiple configurable patterns for supported
    urls.  Based on SimpleHTTPResolver.  Identifiers in URLs should be
    specified as `template_name:id`.

    The configuration MUST contain
     * `cache_root`, which is the absolute path to the directory where source images
        should be cached.

    The configuration SHOULD contain
     * `templates`, a comma-separated list of template names e.g.
        templates=`site1,site2`
     * A subsection named for each template, e.g. `[[site1]]`. This subsection
       MUST contain a `url`, which is a url pattern for each specified template, e.g.
       url='http://example.edu/images/%s' or
       url='http://example.edu/images/%s/master'. It MAY also contain other keys
       from the SimpleHTTPResolver configuration to provide a per-template
       override of these options. Overridable keys are `user`, `pw`,
       `ssl_check`, `cert`, and `key`.

    Note that if a template is listed but has no pattern configured, the
    resolver will warn but not fail.

    The configuration may also include the following settings, as used by
    SimpleHTTPResolver:
     * `default_format`, the format of images (will use content-type of
        response if not specified).
     * `head_resolvable` with value True, whether to make HEAD requests
        to verify object existence (don't set if using Fedora Commons
        prior to 3.8).  [Currently must be the same for all templates]
    '''
    def __init__(self, config):
        # required for simplehttpresolver
        # all templates are assumed to be uri resolvable
        config['uri_resolvable'] = True
        super(TemplateHTTPResolver, self).__init__(config)
        templates = self.config.get('templates', '')
        # technically it's not an error to have no templates configured,
        # but nothing will resolve; is that useful? or should this
        # cause an exception?
        if not templates:
            logger.warn('No templates specified in configuration')
        self.templates = {}
        for name in templates.split(','):
            name = name.strip()
            cfg = self.config.get(name, None)
            if cfg is None:
                logger.warn('No configuration specified for resolver template %s' % name)
            else:
                self.templates[name] = cfg
        logger.debug('TemplateHTTPResolver templates: %s' % str(self.templates))

    def _web_request_url(self, ident):
        # only split identifiers that look like template ids;
        # ignore other requests (e.g. favicon)
        if ':' not in ident:
            return (None, {})
        prefix, ident = ident.split(':', 1)

        url = None
        if 'delimiter' in self.config:
            # uses delimiter of choice from config file to split identifier
            # into tuple that will be fed to template
            ident_components = ident.split(self.config['delimiter'])
            if prefix in self.templates:
                url = self.templates[prefix]['url'] % tuple(ident_components)
        else:
            if prefix in self.templates:
                url = self.templates[prefix]['url'] % ident
        if url is None:
            # if prefix is not recognized, no identifier is returned
            # and loris will return a 404
            return (None, {})
        else:
            # first get the generic options
            options = self.request_options()
            # then add any template-specific ones
            conf = self.templates[prefix]
            if 'cert' in conf and 'key' in conf:
                options['cert'] = (conf['cert'], conf['key'])
            if 'user' in conf and 'pw' in conf:
                options['auth'] = (conf['user'], conf['pw'])
            if 'ssl_check' in conf:
                options['verify'] = conf['ssl_check']
            return (url, options)


class SourceImageCachingResolver(_AbstractResolver):
    '''
    Example resolver that one might use if image files were coming from
    mounted network storage. The first call to `resolve()` copies the source
    image into a local cache; subsequent calls use local copy from the cache.

    The config dictionary MUST contain
     * `cache_root`, which is the absolute path to the directory where images
        should be cached.
     * `source_root`, the root directory for source images.
    '''
    def __init__(self, config):
        super(SourceImageCachingResolver, self).__init__(config)
        self.cache_root = self.config['cache_root']
        self.source_root = self.config['source_root']

    def is_resolvable(self, ident):
        source_fp = self.source_file_path(ident)
        return exists(source_fp)

    def source_file_path(self, ident):
        ident = unquote(ident)
        return join(self.source_root, ident)

    def cache_file_path(self, ident):
        ident = unquote(ident)
        return join(self.cache_root, ident)

    def in_cache(self, ident):
        return exists(self.cache_file_path(ident))

    def copy_to_cache(self, ident):
        source_fp = self.source_file_path(ident)
        cache_fp = self.cache_file_path(ident)

        makedirs(dirname(cache_fp))
        copy(source_fp, cache_fp)
        logger.info("Copied %s to %s" % (source_fp, cache_fp))

    def raise_404_for_ident(self, ident):
        source_fp = self.source_file_path(ident)
        public_message = 'Source image not found for identifier: %s.' % (ident,)
        log_message = 'Source image not found at %s for identifier: %s.' % (source_fp,ident)
        logger.warn(log_message)
        raise ResolverException(404, public_message)

    def resolve(self, ident):
        if not self.is_resolvable(ident):
            self.raise_404_for_ident(ident)
        if not self.in_cache(ident):
            self.copy_to_cache(ident)

        cache_fp = self.cache_file_path(ident)
        logger.debug('Image Served from local cache: %s' % (cache_fp,))

        format_ = self.format_from_ident(ident)
        return (cache_fp, format_)
