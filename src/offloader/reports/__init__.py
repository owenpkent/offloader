"""Report writers: PDF, CSV, MHL, ASC MHL, HTML."""

from .csv_report import write_csv  # noqa: F401
from .html import write_html  # noqa: F401
from .mhl import write_mhl  # noqa: F401
from ..ascmhl import write_ascmhl  # noqa: F401
from .pdf import write_pdf  # noqa: F401

#: Format key -> writer. The CLI's --report flag indexes this.
WRITERS = {
    "pdf": write_pdf,
    "csv": write_csv,
    "mhl": write_mhl,
    "ascmhl": write_ascmhl,
    "html": write_html,
}
