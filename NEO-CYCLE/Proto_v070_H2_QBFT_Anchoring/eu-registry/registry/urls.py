from django.urls import path
from . import views

urlpatterns = [
    path('', views.search, name='search'),
    path('api/register/', views.register, name='register'),
    path('01/<str:gtin>', views.resolve_gtin, name='resolve_gtin'),
]
