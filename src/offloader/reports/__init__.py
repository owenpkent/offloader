"""Report writers: PDF, CSV, MHL, HTML."""

from .csv_report import write_csv  # noqa: F401
from .html import write_html  # noqa: F401
from .mhl import write_mhl  # noqa: F401
from .pdf import write_pdf  # noqa: F401

#: Format key -> writer. The CLI's --report flag indexes this.
WRITERS = {
    "pdf": write_pdf,
    "csv": write_csv,
    "mhl": write_mhl,
    "html": write_html,
}
