# Developer Notes - Warana Milk

## Codebase Overview
- Django app: bakery/
- Settings: warana_milk/settings.py
- URLs: warana_milk/urls.py, bakery/urls.py
- Templates: bakery/templates/
- Static: static/bakery/
- Tests: bakery/tests.py (78 tests)

## Key Modules
- models.py - data models
- views.py - request handlers
- billing.py - billing logic & stock deduction
- forms.py - forms/validation
- exports.py - exports/receipts
- purchasing.py - purchasing flows
- context_processors.py - template context
- templatetags/bakery_tags.py - custom template tags

## Dev Workflow
1. Activate venv: .\\venv\\Scripts\\activate
2. Run checks: python manage.py check
3. Run tests: python manage.py test
4. Start server: python manage.py runserver 8000
5. Before commit: run check + test

## Useful Commands
- Shell: python manage.py shell
- Create superuser: python manage.py createsuperuser
- Collect static: python manage.py collectstatic --noinput
- Import images: python manage.py import_product_images --folder <path> --dry-run

## Code Quality
- Syntax verified with py_compile/compileall
- All existing tests pass
- Follow existing style (no extra comments unless requested)

