from pathlib import Path

import pypdf


def main():
    pdf = pypdf.PdfWriter()

    page = pypdf.PageObject.create_blank_page(None, 612, 792)  # 创建一个标准页面大小
    pdf.add_page(page)
    pdf.add_outline_item("示例 - 书签", 1, parent=None)

    try:
        dest_file = Path(__file__).parent / "example.pdf"
        with dest_file.open("wb") as f:
            pdf.write(f)
    except Exception as e:
        print(f"文件生成失败: {e}")
    else:
        print(f"文件生成成功: {dest_file}")


if __name__ == "__main__":
    main()
