"""Building blocks for the paper PDF artifact and analysis pipeline.

The package deliberately keeps optional PDF dependencies lazy: importing
``PaperPipeline`` does not import :mod:`pypdf`.
"""

from PaperPipeline.taxonomy import (
    DuplicateTaxonomyCodeError,
    EmptyTaxonomyError,
    MalformedTaxonomyError,
    Taxonomy,
    TaxonomyEntry,
    TaxonomyError,
    load_level3_taxonomy,
    load_taxonomy,
)
from PaperPipeline.text_extractor import (
    EncryptedPdfError,
    InsufficientTextError,
    InvalidPdfError,
    PdfDependencyError,
    TextExtractionError,
    TextExtractionResult,
    extract_pdf_text,
    extract_pdf_to_text,
)

__all__ = [
    "DuplicateTaxonomyCodeError",
    "EmptyTaxonomyError",
    "EncryptedPdfError",
    "InsufficientTextError",
    "InvalidPdfError",
    "MalformedTaxonomyError",
    "PdfDependencyError",
    "Taxonomy",
    "TaxonomyEntry",
    "TaxonomyError",
    "TextExtractionError",
    "TextExtractionResult",
    "extract_pdf_text",
    "extract_pdf_to_text",
    "load_level3_taxonomy",
    "load_taxonomy",
]
