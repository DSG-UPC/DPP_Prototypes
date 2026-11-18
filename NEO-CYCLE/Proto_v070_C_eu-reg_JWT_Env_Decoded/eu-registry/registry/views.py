import json

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

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
