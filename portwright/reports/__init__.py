"""PDF / HTML report generation for portwright.

Usage (CLI):
    portwright report run <name>            # → <output>.pdf
    portwright report run <name> --html     # → <output>.html (preview)

Requires the 'reports' extra:
    pip install portwright[reports]         # installs jinja2 + weasyprint
"""
from .service import ReportService

__all__ = ["ReportService"]
