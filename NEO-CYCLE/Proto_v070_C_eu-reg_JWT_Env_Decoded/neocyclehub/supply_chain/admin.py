from django.contrib import admin
from .models import Actor, ProductBatch, ProcessingEvent, ConformityClaim, IssuedCredential


@admin.register(Actor)
class ActorAdmin(admin.ModelAdmin):
    list_display = ('name', 'role', 'did')
    list_filter = ('role',)


@admin.register(ProductBatch)
class ProductBatchAdmin(admin.ModelAdmin):
    list_display = ('name', 'actor', 'batch_number', 'cpc_name', 'upstream_process_type')
    list_filter = ('actor', 'upstream_process_type')
    fieldsets = [
        (None, {'fields': [
            'actor', 'name', 'description', 'gtin',
            'model_number', 'batch_number', 'id_granularity',
            'cpc_code', 'cpc_name',
        ]}),
        ('Material Provenance (DPP → materialProvenance)', {'fields': [
            'recycled_mass_fraction',
        ], 'classes': ['collapse']}),
        ('Upstream Process (DPP → characteristics.upstreamProcess)', {'fields': [
            'upstream_process_type', 'upstream_source_material',
            'upstream_source_batch', 'upstream_process_date',
        ], 'classes': ['collapse']}),
    ]


@admin.register(ProcessingEvent)
class ProcessingEventAdmin(admin.ModelAdmin):
    list_display = ('name', 'actor', 'event_type', 'event_date')
    list_filter = ('actor', 'event_type')
    filter_horizontal = ('input_products', 'output_products')


@admin.register(ConformityClaim)
class ConformityClaimAdmin(admin.ModelAdmin):
    list_display = ('name', 'actor', 'standard_name', 'valid_from', 'valid_until')
    list_filter = ('actor',)


@admin.register(IssuedCredential)
class IssuedCredentialAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'actor', 'cred_type', 'label', 'issued_at')
    list_filter = ('actor', 'cred_type')
    readonly_fields = ('uuid', 'issued_at', 'credential')
