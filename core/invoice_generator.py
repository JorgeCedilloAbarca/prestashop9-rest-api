"""
invoice_generator.py — Generador de facturas PDF para el microservicio PrestaShop
Usa reportlab para generar un PDF profesional con los datos de la factura de PS.
"""

import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)


# ── Paleta de colores ──────────────────────────────────────────
COLOR_PRIMARY   = colors.HexColor("#1e293b")   # gris oscuro — títulos
COLOR_ACCENT    = colors.HexColor("#2563eb")   # azul — cabeceras tabla
COLOR_ACCENT_BG = colors.HexColor("#eff6ff")   # azul claro — fondo cabecera
COLOR_MUTED     = colors.HexColor("#64748b")   # gris medio — textos secundarios
COLOR_BORDER    = colors.HexColor("#e2e8f0")   # gris claro — bordes
COLOR_SUCCESS   = colors.HexColor("#16a34a")   # verde — total
COLOR_WHITE     = colors.white
COLOR_ROW_ALT   = colors.HexColor("#f8fafc")   # gris muy claro — filas alternas


def generar_factura_pdf(factura: dict, pedido: dict, productos: list, tienda: dict) -> bytes:
    """
    Genera un PDF de factura profesional.

    Args:
        factura: dict con id, numero, fecha, total_sin_iva, total_con_iva, total_envio
        pedido:  dict con order_id, reference, customer, date_add, currency
        productos: list de dicts con nombre, referencia, cantidad, precio_unit, total, tasa_iva
        tienda:  dict con nombre, direccion (opcional)

    Returns:
        bytes del PDF generado
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=20*mm,
        bottomMargin=20*mm,
    )

    width = A4[0] - 40*mm   # ancho útil
    styles = getSampleStyleSheet()

    # ── Estilos personalizados ─────────────────────────────────
    st_title = ParagraphStyle(
        "titulo",
        fontSize=22, fontName="Helvetica-Bold",
        textColor=COLOR_PRIMARY, alignment=TA_RIGHT,
    )
    st_factura_num = ParagraphStyle(
        "factura_num",
        fontSize=11, fontName="Helvetica",
        textColor=COLOR_MUTED, alignment=TA_RIGHT,
    )
    st_tienda = ParagraphStyle(
        "tienda",
        fontSize=13, fontName="Helvetica-Bold",
        textColor=COLOR_ACCENT,
    )
    st_label = ParagraphStyle(
        "label",
        fontSize=8, fontName="Helvetica",
        textColor=COLOR_MUTED,
    )
    st_value = ParagraphStyle(
        "value",
        fontSize=9, fontName="Helvetica",
        textColor=COLOR_PRIMARY,
    )
    st_section = ParagraphStyle(
        "section",
        fontSize=9, fontName="Helvetica-Bold",
        textColor=COLOR_MUTED, spaceBefore=4,
    )
    st_total_label = ParagraphStyle(
        "total_label",
        fontSize=10, fontName="Helvetica-Bold",
        textColor=COLOR_PRIMARY, alignment=TA_RIGHT,
    )
    st_total_value = ParagraphStyle(
        "total_value",
        fontSize=12, fontName="Helvetica-Bold",
        textColor=COLOR_SUCCESS, alignment=TA_RIGHT,
    )
    st_footer = ParagraphStyle(
        "footer",
        fontSize=7, fontName="Helvetica",
        textColor=COLOR_MUTED, alignment=TA_CENTER,
    )

    story = []

    # ══════════════════════════════════════════════════════════
    # CABECERA — nombre tienda + FACTURA + número
    # ══════════════════════════════════════════════════════════
    numero_str = f"#FA{str(factura.get('numero', factura.get('id', '?'))).zfill(6)}"
    fecha_str  = factura.get("fecha", pedido.get("date_add", ""))[:10]

    header_data = [[
        Paragraph(tienda.get("nombre", "Mi Tienda"), st_tienda),
        Table([
            [Paragraph("FACTURA", st_title)],
            [Paragraph(fecha_str, st_factura_num)],
            [Paragraph(numero_str, st_factura_num)],
        ], colWidths=[width * 0.45])
    ]]
    header_table = Table(header_data, colWidths=[width * 0.55, width * 0.45])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width=width, thickness=0.5, color=COLOR_BORDER))
    story.append(Spacer(1, 5*mm))

    # ══════════════════════════════════════════════════════════
    # DATOS DEL PEDIDO — referencia, fecha, cliente
    # ══════════════════════════════════════════════════════════
    meta_data = [
        [
            Paragraph("NÚMERO DE FACTURA", st_label),
            Paragraph("FECHA DE FACTURA", st_label),
            Paragraph("REFERENCIA PEDIDO", st_label),
            Paragraph("FECHA PEDIDO", st_label),
        ],
        [
            Paragraph(numero_str, st_value),
            Paragraph(fecha_str, st_value),
            Paragraph(pedido.get("reference", "—"), st_value),
            Paragraph(str(pedido.get("date_add", ""))[:10], st_value),
        ],
    ]
    meta_table = Table(meta_data, colWidths=[width/4]*4)
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_ACCENT_BG),
        ("ROWBACKGROUNDS", (0, 1), (-1, 1), [COLOR_WHITE]),
        ("GRID",      (0, 0), (-1, -1), 0.5, COLOR_BORDER),
        ("FONTSIZE",  (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 5*mm))

    # ── Cliente ───────────────────────────────────────────────
    story.append(Paragraph("CLIENTE", st_section))
    story.append(Spacer(1, 1*mm))
    cliente_data = [[
        Paragraph(pedido.get("customer", "—"), st_value),
        Paragraph(f"ID Pedido: #{pedido.get('order_id', '?')}", st_value),
    ]]
    cliente_table = Table(cliente_data, colWidths=[width*0.6, width*0.4])
    cliente_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_ROW_ALT),
        ("BOX",        (0, 0), (-1, -1), 0.5, COLOR_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    story.append(cliente_table)
    story.append(Spacer(1, 5*mm))

    # ══════════════════════════════════════════════════════════
    # TABLA DE PRODUCTOS
    # ══════════════════════════════════════════════════════════
    col_ref     = width * 0.12
    col_nombre  = width * 0.38
    col_iva     = width * 0.10
    col_precio  = width * 0.14
    col_qty     = width * 0.10
    col_total   = width * 0.16

    st_th = ParagraphStyle("th", fontSize=8, fontName="Helvetica-Bold",
                           textColor=COLOR_WHITE, alignment=TA_CENTER)
    st_td = ParagraphStyle("td", fontSize=8, fontName="Helvetica",
                           textColor=COLOR_PRIMARY)
    st_td_r = ParagraphStyle("td_r", fontSize=8, fontName="Helvetica",
                              textColor=COLOR_PRIMARY, alignment=TA_RIGHT)
    st_td_c = ParagraphStyle("td_c", fontSize=8, fontName="Helvetica",
                              textColor=COLOR_PRIMARY, alignment=TA_CENTER)

    prod_data = [[
        Paragraph("REF.", st_th),
        Paragraph("PRODUCTO", st_th),
        Paragraph("IVA", st_th),
        Paragraph("P. UNIT.", st_th),
        Paragraph("CANT.", st_th),
        Paragraph("TOTAL", st_th),
    ]]

    for i, p in enumerate(productos):
        tasa = p.get("tasa_iva", 21)
        prod_data.append([
            Paragraph(p.get("referencia", ""), st_td),
            Paragraph(p.get("nombre", ""), st_td),
            Paragraph(f"{tasa} %", st_td_c),
            Paragraph(f"{p.get('precio_unit', 0):.2f} €", st_td_r),
            Paragraph(str(p.get("cantidad", 0)), st_td_c),
            Paragraph(f"{p.get('total', 0):.2f} €", st_td_r),
        ])

    prod_table = Table(
        prod_data,
        colWidths=[col_ref, col_nombre, col_iva, col_precio, col_qty, col_total],
        repeatRows=1,
    )
    row_colors = []
    for i in range(1, len(prod_data)):
        bg = COLOR_WHITE if i % 2 == 0 else COLOR_ROW_ALT
        row_colors.append(("BACKGROUND", (0, i), (-1, i), bg))

    prod_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), COLOR_ACCENT),
        ("GRID",          (0, 0), (-1, -1), 0.5, COLOR_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ] + row_colors))
    story.append(prod_table)
    story.append(Spacer(1, 5*mm))

    # ══════════════════════════════════════════════════════════
    # RESUMEN FINANCIERO
    # ══════════════════════════════════════════════════════════
    total_sin_iva = float(factura.get("total_sin_iva", 0))
    total_con_iva = float(factura.get("total_con_iva", 0))
    total_envio   = float(factura.get("total_envio", 0))
    total_iva     = total_con_iva - total_sin_iva

    st_sum_label = ParagraphStyle("sl", fontSize=9, fontName="Helvetica",
                                   textColor=COLOR_MUTED, alignment=TA_RIGHT)
    st_sum_value = ParagraphStyle("sv", fontSize=9, fontName="Helvetica",
                                   textColor=COLOR_PRIMARY, alignment=TA_RIGHT)

    resumen_data = [
        [Paragraph("Subtotal (sin IVA)", st_sum_label), Paragraph(f"{total_sin_iva:.2f} €", st_sum_value)],
        [Paragraph("IVA",               st_sum_label), Paragraph(f"{total_iva:.2f} €",     st_sum_value)],
        [Paragraph("Gastos de envío",   st_sum_label), Paragraph(f"{total_envio:.2f} €" if total_envio else "Gratis", st_sum_value)],
        [Paragraph("<b>TOTAL</b>",      ParagraphStyle("stb", fontSize=11, fontName="Helvetica-Bold",
                                                        textColor=COLOR_PRIMARY, alignment=TA_RIGHT)),
         Paragraph(f"<b>{total_con_iva:.2f} €</b>",
                   ParagraphStyle("stv", fontSize=11, fontName="Helvetica-Bold",
                                  textColor=COLOR_SUCCESS, alignment=TA_RIGHT))],
    ]

    resumen_table = Table(resumen_data, colWidths=[width*0.6, width*0.4],
                          hAlign="RIGHT")
    resumen_table.setStyle(TableStyle([
        ("LINEABOVE",     (0, -1), (-1, -1), 1.5, COLOR_ACCENT),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(resumen_table)
    story.append(Spacer(1, 8*mm))

    # ══════════════════════════════════════════════════════════
    # PIE DE PÁGINA
    # ══════════════════════════════════════════════════════════
    story.append(HRFlowable(width=width, thickness=0.5, color=COLOR_BORDER))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"{tienda.get('nombre', 'Mi Tienda')} — Factura generada automáticamente por el microservicio PrestaShop",
        st_footer,
    ))

    doc.build(story)
    return buffer.getvalue()
