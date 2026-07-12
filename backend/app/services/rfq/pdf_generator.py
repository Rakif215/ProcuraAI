import os
import sys
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import text

# Insert the parent backend directory at index 0 to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import engine

# Import ReportLab elements
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas to dynamically compute and render "Page X of Y" page numbers in the footer.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        
        # 1. Header rule & company watermark
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(36, A4[1] - 36, A4[0] - 36, A4[1] - 36)
        
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor("#0F172A"))
        self.drawString(36, A4[1] - 30, "APEX INDUSTRIAL SUPPLIES WLL")
        
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        self.drawRightString(A4[0] - 36, A4[1] - 30, "Industrial Products & Piping Solutions")
        
        # 2. Footer rule & page numbering
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.line(36, 45, A4[0] - 36, 45)
        
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        self.drawString(36, 32, "Tel: +974 4455 6677 | Email: sales@apexsuppliesqa.com | Doha, Qatar")
        
        page_num_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(A4[0] - 36, 32, page_num_str)
        
        self.restoreState()


async def generate_quote_pdf(quote_number: str) -> str:
    # 1. Query quote details from Supabase
    async with engine.begin() as conn:
        quote_res = await conn.execute(
            text("""
                SELECT 
                    q.id,
                    q.total_amount,
                    q.status,
                    q.created_at,
                    c.buyer_name,
                    c.buyer_company,
                    c.rfq_ref,
                    c.id as conv_id
                FROM apex_quotations q
                JOIN apex_conversations c ON q.conversation_id = c.id
                WHERE q.quote_number = :quote_number;
            """),
            {"quote_number": quote_number}
        )
        quote = quote_res.first()
        if not quote:
            raise ValueError(f"Quotation {quote_number} not found in database.")
            
        quote_id, total_amount, status, created_at, buyer_name, buyer_company, rfq_ref, conv_id = quote
        
        # Fetch items
        items_res = await conn.execute(
            text("""
                SELECT 
                    qli.item_name,
                    qli.specification,
                    qli.quantity_quoted,
                    qli.unit_price,
                    qli.total_price,
                    qli.match_status,
                    qli.shortage_quantity,
                    rli.unit
                FROM apex_quotation_line_items qli
                LEFT JOIN apex_rfq_line_items rli ON qli.rfq_line_item_id = rli.id
                WHERE qli.quotation_id = :quote_id;
            """),
            {"quote_id": quote_id}
        )
        items = items_res.all()

    # Create directories if not existing
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_dir = os.path.join(base_dir, 'generated_quotes')
    os.makedirs(output_dir, exist_ok=True)
    pdf_path = os.path.join(output_dir, f"{quote_number}.pdf")
    
    # 2. Setup styles
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0F172A")
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#0D9488")
    )
    
    meta_label_style = ParagraphStyle(
        'MetaLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#475569")
    )
    
    meta_val_style = ParagraphStyle(
        'MetaValue',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#1E293B")
    )

    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#0F172A")
    )
    
    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.white
    )
    
    table_body_style = ParagraphStyle(
        'TableBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#1E293B")
    )

    table_body_bold = ParagraphStyle(
        'TableBodyBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#1E293B")
    )

    terms_style = ParagraphStyle(
        'TermsStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569")
    )

    story = []
    
    # Spacing from watermark header
    story.append(Spacer(1, 15))
    
    # 3. Document Title / Brand Header Table
    header_data = [
        [
            Paragraph("QUOTATION", title_style),
            Paragraph("<b>Apex Industrial Supplies WLL</b><br/>Building 42, Street 810, Industrial Area<br/>Doha, State of Qatar", subtitle_style)
        ]
    ]
    header_table = Table(header_data, colWidths=[3.0*inch, 4.0*inch])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (1,0), (1,0), 'RIGHT'),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 15))
    
    # 4. Meta Information Grid (2-column details block)
    issue_date = created_at.strftime("%d %b %Y") if created_at else datetime.now().strftime("%d %b %Y")
    expiry_date = (created_at + timedelta(days=15)).strftime("%d %b %Y") if created_at else (datetime.now() + timedelta(days=15)).strftime("%d %b %Y")
    
    meta_data = [
        [
            Paragraph("Quote Number:", meta_label_style), Paragraph(quote_number, meta_val_style),
            Paragraph("Client Company:", meta_label_style), Paragraph(buyer_company or "Unknown", meta_val_style)
        ],
        [
            Paragraph("Date of Issue:", meta_label_style), Paragraph(issue_date, meta_val_style),
            Paragraph("Attention:", meta_label_style), Paragraph(buyer_name or "Procurement Manager", meta_val_style)
        ],
        [
            Paragraph("Valid Until:", meta_label_style), Paragraph(expiry_date, meta_val_style),
            Paragraph("RFQ Reference:", meta_label_style), Paragraph(rfq_ref or "N/A", meta_val_style)
        ],
        [
            Paragraph("Currency:", meta_label_style), Paragraph("Qatari Riyal (QAR)", meta_val_style),
            Paragraph("Payment Terms:", meta_label_style), Paragraph("30 Days Net", meta_val_style)
        ]
    ]
    meta_table = Table(meta_data, colWidths=[1.2*inch, 2.1*inch, 1.2*inch, 2.5*inch])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#F1F5F9")),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 20))
    
    # 5. Items Table Section
    story.append(Paragraph("Line Item Details", section_header_style))
    story.append(Spacer(1, 8))
    
    table_data = [
        [
            Paragraph("S.No", table_header_style),
            Paragraph("Item Description & Specification", table_header_style),
            Paragraph("Qty", table_header_style),
            Paragraph("Unit", table_header_style),
            Paragraph("Unit Price", table_header_style),
            Paragraph("Total Price", table_header_style),
            Paragraph("Availability", table_header_style)
        ]
    ]
    
    for s_no, item in enumerate(items, 1):
        qty = float(item[2])
        unit_price = float(item[3])
        total_p = float(item[4])
        match_status = item[5]
        
        status_color = "#0D9488" # Teal for FULL_STOCK
        if match_status == "PARTIAL_STOCK":
            status_color = "#EA580C" # Orange
        elif match_status == "OUT_OF_STOCK":
            status_color = "#DC2626" # Red
            
        status_p = f"<font color='{status_color}'><b>{match_status.replace('_', ' ')}</b></font>"
        if match_status == "PARTIAL_STOCK" and item[6] > 0:
            status_p += f"<br/><font color='#64748B' size='6'>Short: {float(item[6]):g}</font>"
            
        desc_p = f"<b>{item[0]}</b><br/><font color='#475569'>{item[1] or ''}</font>"
        
        table_data.append([
            Paragraph(str(s_no), table_body_style),
            Paragraph(desc_p, table_body_style),
            Paragraph(f"{qty:g}", table_body_style),
            Paragraph(item[7] if item[7] else "pcs", table_body_style),
            Paragraph(f"{unit_price:,.2f} QAR", table_body_style),
            Paragraph(f"{total_p:,.2f} QAR", table_body_bold),
            Paragraph(status_p, table_body_style)
        ])
        
    # Table styling
    # Column widths: Total = 7.0 inches (A4 width - margins is 7.27 inches)
    # A4 printable area width is 595 - 72 = 523pt = 7.26 inches
    col_widths = [0.3*inch, 2.7*inch, 0.5*inch, 0.5*inch, 1.1*inch, 1.1*inch, 1.0*inch]
    
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    
    t_style = [
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#0F172A")),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#E2E8F0")),
    ]
    
    # Alternating row colors
    for i in range(1, len(table_data)):
        if i % 2 == 0:
            t_style.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor("#F8FAFC")))
            
    items_table.setStyle(TableStyle(t_style))
    story.append(items_table)
    story.append(Spacer(1, 15))
    
    # 6. Total Summary block aligning right
    total_data = [
        [Paragraph("<b>Subtotal:</b>", meta_label_style), Paragraph(f"{total_amount:,.2f} QAR", meta_val_style)],
        [Paragraph("<b>VAT (0%):</b>", meta_label_style), Paragraph("0.00 QAR", meta_val_style)],
        [Paragraph("<b>Total Amount:</b>", ParagraphStyle('TTotal', parent=meta_label_style, fontSize=11, textColor=colors.HexColor("#0F172A"))), Paragraph(f"<b>{total_amount:,.2f} QAR</b>", ParagraphStyle('TTotalVal', parent=meta_val_style, fontSize=11, textColor=colors.HexColor("#0D9488")))]
    ]
    total_table = Table(total_data, colWidths=[1.5*inch, 1.5*inch])
    total_table.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('LINEBELOW', (0,0), (-1,-2), 0.5, colors.HexColor("#E2E8F0")),
    ]))
    
    # Place summary aligned to the right by putting it inside a wrapping table
    summary_wrapper_data = [[Spacer(1,1), total_table]]
    summary_wrapper = Table(summary_wrapper_data, colWidths=[4.5*inch, 3.0*inch])
    summary_wrapper.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (1,0), (1,0), 'RIGHT')
    ]))
    story.append(summary_wrapper)
    story.append(Spacer(1, 20))
    
    # 7. Terms & Conditions Block
    story.append(Paragraph("Terms & Conditions", section_header_style))
    story.append(Spacer(1, 6))
    
    terms_text = """
    1. <b>Validity:</b> This quotation is valid for 15 days from the date of issue.<br/>
    2. <b>Delivery:</b> Materials will be delivered to site as per standard lead times unless specified otherwise.<br/>
    3. <b>Payment:</b> Net 30 days from date of invoice.<br/>
    4. <b>Stock Status:</b> Items marked as <i>PARTIAL STOCK</i> or <i>OUT OF STOCK</i> are subject to factory lead times (typically 7-14 days). Please confirm prior to placing purchase orders.
    """
    story.append(Paragraph(terms_text, terms_style))
    story.append(Spacer(1, 40))
    
    # 8. Signature Block
    sig_data = [
        [
            Paragraph("Prepared By:<br/><br/>___________________________<br/>Sales & Quotations Department<br/>Apex Industrial Supplies", terms_style),
            Paragraph("Accepted By (Buyer Signature):<br/><br/>___________________________<br/>Date: ____ / ____ / ________<br/>Stamp:", terms_style)
        ]
    ]
    sig_table = Table(sig_data, colWidths=[3.5*inch, 3.5*inch])
    story.append(sig_table)
    
    # 9. Build Document
    # Margin settings (36pt = 0.5 inch margins)
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=54,
        bottomMargin=54
    )
    
    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Graphical PDF generated successfully at: {pdf_path}")
    return pdf_path

if __name__ == "__main__":
    # Test generation for QT-2026-003
    asyncio.run(generate_quote_pdf("QT-2026-003"))
