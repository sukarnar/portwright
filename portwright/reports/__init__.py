"""Report generation for portwright: PDF, HTML, Markdown, CSV, Excel and JSON.

Usage (CLI):
    portwright report run <name>                    # the report's formats (default: pdf)
    portwright report run <name> -f pdf -f xlsx     # several formats in one run
    portwright report run <name> --html             # HTML only (preview)

PDF/HTML need the 'reports' extra (jinja2 + weasyprint):
    pip install portwright[reports]
CSV, XLSX, JSON and Markdown need nothing extra.
"""
from .renderers import RENDERERS, ReportArtifact
from .service import ReportResult, ReportService

__all__ = ["ReportService", "ReportResult", "ReportArtifact", "RENDERERS"]
