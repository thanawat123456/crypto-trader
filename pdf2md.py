from pathlib import Path
import fitz  # PyMuPDF
import pymupdf4llm

pdf_file = "My_Learnings.pdf"
out_md = Path("My_Learnings.md")
img_dir = Path("images")
img_dir.mkdir(exist_ok=True)

# 1) แปลงข้อความ PDF เป็น Markdown
markdown = pymupdf4llm.to_markdown(pdf_file)

# 2) export ทุกหน้า PDF เป็นรูป
doc = fitz.open(pdf_file)

image_links = []

for i, page in enumerate(doc, start=1):
    pix = page.get_pixmap(dpi=200)
    img_path = img_dir / f"page_{i:03}.png"
    pix.save(img_path)

    image_links.append(f"\n\n## Page {i}\n\n![Page {i}]({img_path.as_posix()})\n")

# 3) รวมข้อความ + รูปหน้า PDF
final_md = markdown + "\n\n---\n\n# PDF Page Images\n" + "".join(image_links)

out_md.write_text(final_md, encoding="utf-8")

print("Done")
print(f"Markdown: {out_md}")
print(f"Images: {img_dir}")