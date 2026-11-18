from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('actors/<int:pk>/', views.actor_detail, name='actor_detail'),
    path('actors/<int:pk>/edit/', views.edit_actor, name='edit_actor'),
    path('credentials/<uuid:uuid>/', views.serve_credential, name='serve_credential'),
    path('credentials/<uuid:uuid>/chain/', views.credential_chain, name='credential_chain'),
    path('credentials/<uuid:uuid>/decoded/', views.decode_credential, name='decode_credential'),
    path('credentials/<uuid:uuid>/delete/', views.delete_credential, name='delete_credential'),

    # Issue actions
    path('issue/dpp/<int:product_pk>/', views.issue_dpp, name='issue_dpp'),
    path('issue/dte/<int:event_pk>/', views.issue_dte, name='issue_dte'),
    path('issue/dcc/<int:claim_pk>/', views.issue_dcc, name='issue_dcc'),
    path('issue/dfr/<int:actor_pk>/', views.issue_dfr, name='issue_dfr'),
    path('issue/dia/<int:actor_pk>/', views.issue_dia, name='issue_dia'),
]
