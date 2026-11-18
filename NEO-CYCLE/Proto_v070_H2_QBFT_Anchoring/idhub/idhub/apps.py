import os
import requests as _req
import pyvckit.sign as _pv_sign
from django.apps import AppConfig


def _patch_pyvckit_loader():
    """
    Replace pyvckit's create_loader with one that rewrites HTTPS localhost URLs
    to HTTP+port, so JSON-LD context fetches work in the demo where idhub runs
    on http://localhost:PORT rather than https://localhost:443.
    """
    from django.conf import settings
    domain = settings.DOMAIN
    port = os.environ.get('PORT', '8000')
    prefix_https = f"https://{domain}/"
    prefix_http = f"http://{domain}:{port}/"

    def _loader(url, options={}, verify=True):
        target = url.replace(prefix_https, prefix_http, 1) if url.startswith(prefix_https) else url
        response = _req.get(
            target,
            headers={'Accept': 'application/ld+json, application/json'},
            verify=verify,
        )
        response.raise_for_status()
        return {"contextUrl": None, "document": response.json(), "documentUrl": response.url}

    _pv_sign.create_loader = _loader


class IdhubConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'idhub'

    def ready(self):
        _patch_pyvckit_loader()
