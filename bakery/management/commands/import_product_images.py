"""Bulk-attach product photos from a folder by matching filenames to product names.

    python manage.py import_product_images --folder "C:/Users/me/Desktop/bakery-shots"
    python manage.py import_product_images --folder ./shots --dry-run

Matching ignores case, spaces, hyphens and underscores, so "Milk Bread",
"milk-bread" and "milk_bread.jpg" all match the same product.
"""

import re
import sys
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bakery.models import Product

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Longest first, so "waranamilk" is stripped before "warana".
BRAND_PREFIXES = ("waranamilk", "gokulmilk", "warana", "gokul")


def normalise(text):
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


class Command(BaseCommand):
    help = "Attach product photos from a folder by matching file names to product names."

    def add_arguments(self, parser):
        parser.add_argument("--folder", required=True, help="Folder containing the images.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would happen only.")

    def handle(self, *args, **options):
        folder = Path(options["folder"]).expanduser()
        if not folder.is_dir():
            raise CommandError(f"Not a folder: {folder}")

        files = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in EXTENSIONS
        )
        if not files:
            raise CommandError(
                f"No images found in {folder}. Supported: {', '.join(sorted(EXTENSIONS))}"
            )

        products = {normalise(p.name): p for p in Product.objects.all()}
        by_name = {p.name: p for p in products.values()}

        matched, unmatched_files, unmatched_products = [], [], set(by_name)

        for path in files:
            stem = normalise(path.stem)
            product = products.get(stem)
            if product is None:
                # tolerate a brand prefix, e.g. "Warana Milk Bread.jpg",
                # "warana-milk-bread.jpg", "Gokul Milk Bread.jpg"
                for prefix in BRAND_PREFIXES:
                    if not stem.startswith(prefix):
                        continue
                    trimmed = stem[len(prefix):]
                    # the brand prefix often eats the word "milk" too
                    product = products.get(trimmed) or products.get("milk" + trimmed)
                    if product is not None:
                        break
            if product is None:
                unmatched_files.append(path)
                continue
            matched.append((path, product))
            unmatched_products.discard(product.name)

        if not matched:
            self.stderr.write(self.style.ERROR("Nothing matched. File names must match product names."))
            self._report(unmatched_files, [])
            return

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes saved.\n"))
            for path, product in matched:
                self.stdout.write(f"  {path.name}  ->  {product.name}")
            self._report(unmatched_files, sorted(unmatched_products))
            return

        with transaction.atomic():
            for path, product in matched:
                with path.open("rb") as fh:
                    product.image.save(path.name, File(fh), save=True)
                self.stdout.write(f"  {path.name}  ->  {product.name}")

        self.stdout.write(
            self.style.SUCCESS(f"Attached {len(matched)} image(s).")
        )
        self._report(unmatched_files, sorted(unmatched_products))

    def _report(self, unmatched_files, unmatched_products):
        if unmatched_files:
            self.stdout.write(self.style.WARNING("\nFiles with no matching product:"))
            for path in unmatched_files:
                self.stdout.write(f"  - {path.name}")
            self.stdout.write("  Rename the file to match the product name, or add the product first.")
        if unmatched_products:
            self.stdout.write(self.style.WARNING("\nProducts with no image (themed tile is used):"))
            for name in unmatched_products:
                self.stdout.write(f"  - {name}")
