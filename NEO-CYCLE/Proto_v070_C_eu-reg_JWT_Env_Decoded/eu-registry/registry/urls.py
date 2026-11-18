from django.urls import path
from . import views

urlpatterns = [
    path('', views.search, name='search'),
    path('api/register/', views.register, name='register'),
]
