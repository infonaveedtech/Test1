import os
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, select_autoescape, TemplateNotFound

# Where templates live (reports/templates/report.html)
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

_jenv = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

def render_report_html(context: dict) -> str:
    """
    Render the appropriate HTML template with the given context.

    - If context["layout_type"] == "grouped", try report_grouped.html first.
      If that template doesn't exist, fall back to report.html.
    - Otherwise use report.html.
    """
    layout_type = (context or {}).get("layout_type") or "flat"
    layout_type = str(layout_type).lower()

    if layout_type == "grouped":
        try:
            tpl = _jenv.get_template("report_grouped.html")
        except TemplateNotFound:
            tpl = _jenv.get_template("report.html")
    else:
        tpl = _jenv.get_template("report.html")

    return tpl.render(**context)


try:
    import pdfkit
except ImportError:
    pdfkit = None
    
def _get_pdfkit_config():
    """
    Build a pdfkit configuration using WKHTMLTOPDF_PATH env var if provided.
    This is needed on Windows where wkhtmltopdf is not usually on PATH.
    """
    if pdfkit is None:
        return None

    wkhtml_path = os.getenv("WKHTMLTOPDF_PATH")
    if wkhtml_path:
        return pdfkit.configuration(wkhtmltopdf=wkhtml_path)
    # Fallback: let pdfkit try default PATH (may still fail)
    return pdfkit.configuration()


# def html_to_pdf_bytes(html: str) -> bytes:
#     if pdfkit is None:
#         raise RuntimeError("pdfkit is not installed; cannot build PDF.")

#     options = {
#         "page-size": "A4",
#         "margin-top": "10mm",
#         "margin-right": "10mm",
#         "margin-bottom": "10mm",
#         "margin-left": "10mm",
#         "encoding": "UTF-8",
#     }

#     config = _get_pdfkit_config()
#     return pdfkit.from_string(html, False, options=options, configuration=config)

def html_to_pdf_bytes(html: str) -> bytes:
    if pdfkit is None:
        raise RuntimeError("pdfkit is not installed; cannot build PDF.")

    options = {
        "page-size": "A4",
        "margin-top": "10mm",
        "margin-right": "10mm",
        "margin-bottom": "10mm",
        "margin-left": "10mm",
        "encoding": "UTF-8",
        # allow loading local images/css/fonts
        "enable-local-file-access": "",
    }

    config = _get_pdfkit_config()
    return pdfkit.from_string(html, False, options=options, configuration=config)



def build_report_pdf(context: dict) -> bytes:
    """
    Render report.html and turn it into PDF bytes.
    Not used yet in the Streamlit UI, but available.
    """
    html = render_report_html(context)
    return html_to_pdf_bytes(html)
