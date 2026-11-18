import csv
import json
import statistics

from django.db.models import Avg, Count, Max, Min
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import TimingSample

SAMPLE_FIELDS = ["operation", "mode", "source", "cred_type", "actor_name", "label", "chain_depth", "company_count", "chain_position", "duration_ms"]


@csrf_exempt
@require_POST
def record_sample(request):
    """
    Best-effort ingestion endpoint — matches eu-registry's /api/register/
    convention (csrf_exempt, no auth, since it's an internal service-to-
    service call from neocyclehub/neocyclehub-bootstrap, not exposed
    outside the Docker network by default). Accepts either a single sample
    object or a list of them, so a benchmark run can batch-submit.
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    samples = payload if isinstance(payload, list) else [payload]
    created = 0
    errors = []
    for i, s in enumerate(samples):
        if not s.get("operation") or "duration_ms" not in s:
            errors.append(f"sample {i}: missing required field(s) operation/duration_ms")
            continue
        TimingSample.objects.create(
            operation=s["operation"],
            mode=s.get("mode", "passive"),
            source=s.get("source", ""),
            cred_type=s.get("cred_type", ""),
            actor_name=s.get("actor_name", ""),
            label=s.get("label", ""),
            chain_depth=s.get("chain_depth"),
            company_count=s.get("company_count"),
            chain_position=s.get("chain_position", ""),
            duration_ms=s["duration_ms"],
        )
        created += 1

    status = 201 if created else 400
    return JsonResponse({"created": created, "errors": errors}, status=status)


def _filtered_queryset(request):
    qs = TimingSample.objects.all()
    for param, field in [("operation", "operation"), ("mode", "mode"), ("source", "source"), ("chain_position", "chain_position")]:
        value = request.GET.get(param)
        if value:
            qs = qs.filter(**{field: value})
    return qs


@require_GET
def dashboard(request):
    qs = _filtered_queryset(request)

    # Grouped by chain_position too, not just operation+mode — averaging a
    # root-credential walk (one signature check) together with an
    # end-of-chain walk (ten) would hide the exact thing this field exists
    # to show.
    aggregates = []
    max_mean = 0.0
    for row in qs.values("operation", "mode", "chain_position").annotate(
        count=Count("id"), mean=Avg("duration_ms"), lo=Min("duration_ms"), hi=Max("duration_ms")
    ).order_by("operation", "mode", "chain_position"):
        durations = list(
            qs.filter(operation=row["operation"], mode=row["mode"], chain_position=row["chain_position"])
            .values_list("duration_ms", flat=True)
        )
        row["median"] = statistics.median(durations) if durations else 0.0
        aggregates.append(row)
        max_mean = max(max_mean, row["mean"] or 0.0)

    for row in aggregates:
        row["bar_pct"] = round(100 * (row["mean"] or 0.0) / max_mean, 1) if max_mean else 0

    recent = qs[:100]

    return render(request, "metrics/dashboard.html", {
        "aggregates": aggregates,
        "recent": recent,
        "total": qs.count(),
        "operation_choices": TimingSample.OPERATION_CHOICES,
        "mode_choices": TimingSample.MODE_CHOICES,
        "chain_position_choices": TimingSample._meta.get_field("chain_position").choices,
        "selected_operation": request.GET.get("operation", ""),
        "selected_mode": request.GET.get("mode", ""),
        "selected_source": request.GET.get("source", ""),
        "selected_chain_position": request.GET.get("chain_position", ""),
    })


@require_GET
def export_csv(request):
    qs = _filtered_queryset(request)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="timing_samples.csv"'
    writer = csv.writer(response)
    writer.writerow(SAMPLE_FIELDS + ["recorded_at"])
    for s in qs:
        writer.writerow([getattr(s, f) for f in SAMPLE_FIELDS] + [s.recorded_at.isoformat()])
    return response
