from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from bakery.views import UserLoginView, UserLogoutView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', UserLoginView.as_view(), name='login'),
    path('logout/', UserLogoutView.as_view(), name='logout'),
    path('', include('bakery.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
