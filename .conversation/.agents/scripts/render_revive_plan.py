from pathlib import Path

import fitz


pdf_path = Path("attached_assets/ReviveAI_Project_full_plan_1787901477951.pdf")
output_dir = Path(".agents/outputs/revive_plan_pages")
output_dir.mkdir(parents=True, exist_ok=True)

with fitz.open(pdf_path) as document:
    print(f"pages={document.page_count}")
    for page_number, page in enumerate(document, start=1):
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        output_path = output_dir / f"page_{page_number:03d}.png"
        pixmap.save(output_path)
    print(f"rendered={document.page_count} output_dir={output_dir}")