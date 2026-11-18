import json

from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse, HttpResponseRedirect, Http404
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import RegistryEntry

REQUIRED_FIELDS = ["gtin", "product_name", "issuer_did", "issuer_name", "dpp_url"]


@csrf_exempt
@require_POST
def register(request):
    """
    Mock of the mandatory ESPR registration step: "Before a product can be
    placed on the EU market, the relevant economic operator must register its
    DPP in the Registry." Called by neocyclehub right after a DPP is signed —
    stores only index/metadata, never the DPP content itself (that stays at
    dpp_url, hosted by the economic operator).
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    missing = [f for f in REQUIRED_FIELDS if not payload.get(f)]
    if missing:
        return JsonResponse({"error": f"Missing required field(s): {', '.join(missing)}"}, status=400)

    entry = RegistryEntry.objects.create(
        gtin=payload["gtin"],
        product_name=payload["product_name"],
        issuer_did=payload["issuer_did"],
        issuer_name=payload["issuer_name"],
        valid_from=payload.get("valid_from", ""),
        valid_until=payload.get("valid_until", ""),
        dpp_url=payload["dpp_url"],
    )
    return JsonResponse({"registered": True, "id": entry.id}, status=201)


@require_http_methods(["GET", "HEAD"])
def resolve_gtin(request, gtin):
    """
    Minimal GS1 Digital Link resolver. Real
    resolvers live at a path like /01/<GTIN> and content-negotiate: HTML
    (or unspecified) gets a browsable page, application/linkset+json (or
    ?linkType=all) gets a machine-readable IETF Linkset (RFC 9264) response
    listing every available link for that product. This is a minimal
    subset, not a full implementation: the default response is a straight
    redirect to the DPP rather than a browsable multi-link HTML page, and
    only two link relations are offered (pip = product information page,
    and — if the credential UUID can be recovered from dpp_url — a reduced
    public-summary view on neocyclehub). No auth: a resolver is discovery,
    meant to be public.

    RegistryEntry.gtin stores the full GS1 Digital Link URL
    (https://id.gs1.org/01/<GTIN>), not the bare code, so match on the
    trailing segment.
    """
    entry = RegistryEntry.objects.filter(gtin__endswith=f"/01/{gtin}").order_by('-registered_at').first()
    if entry is None:
        raise Http404(f"No registered DPP found for GTIN {gtin}")

    wants_linkset = (
        request.GET.get('linkType') == 'all'
        or 'application/linkset+json' in request.headers.get('Accept', '')
    )
    if not wants_linkset:
        return HttpResponseRedirect(entry.dpp_url)

    links = {
        "linkset": [
            {
                "anchor": request.build_absolute_uri(),
                "itemDescription": entry.product_name,
                "pip": [{"href": entry.dpp_url, "title": "Digital Product Passport (full)"}],
            }
        ]
    }
    public_url = _public_summary_url(entry.dpp_url)
    if public_url:
        links["linkset"][0]["pip"].append(
            {"href": public_url, "title": "Digital Product Passport (public summary)"}
        )
    response = JsonResponse(links)
    response['Content-Type'] = 'application/linkset+json'
    return response


def _public_summary_url(dpp_url):
    """dpp_url looks like http://host:port/credentials/<uuid>/ — the public
    summary view (see neocyclehub's serve_credential_public) lives at the
    same path with /public/ appended. Best-effort string manipulation, not
    a real link-relation registry — good enough for this PoC's one fixed
    pattern."""
    if dpp_url.endswith('/'):
        return f"{dpp_url}public/"
    return None


@require_GET
def search(request):
    query = request.GET.get("q", "").strip()
    results = []
    if query:
        results = RegistryEntry.objects.filter(
            Q(gtin__icontains=query)
            | Q(product_name__icontains=query)
            | Q(issuer_name__icontains=query)
        )[:100]

    return render(request, "registry/search.html", {
        "query": query,
        "results": results,
        "total": RegistryEntry.objects.count(),
    })
