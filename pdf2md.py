from pathlib import Path
import pymupdf4llm

pdf_file = "My_Learnings.pdf"

markdown = pymupdf4llm.to_markdown(
    pdf_file,
    write_images=True,
    image_path="images"
)

Path("output.md").write_text(
    markdown,
    encoding="utf-8"
)

print("Done")