from supply_chain.models import Actor


def sidebar_actors(request):
    try:
        return {'sidebar_actors': Actor.objects.all()}
    except Exception:
        return {'sidebar_actors': []}
