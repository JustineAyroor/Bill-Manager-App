import gradio as gr
from app.ui.screens import ui_dashboard, ui_members, ui_invoices, ui_payments,ui_reminders,ui_applications
from app.ui.bill_import import ui_bill_import

def build_app():
    with gr.Blocks(title="T-Mobile Bill Manager") as demo:
        gr.Markdown("# T-Mobile Bill Manager (Local MVP)")

        with gr.Tab("Dashboard"):
            ui_dashboard(demo)

        with gr.Tab("Members"):
            ui_members(demo)

        with gr.Tab("Invoices & Allocations"):
            ui_invoices(demo)

        with gr.Tab("Payments"):
            ui_payments(demo)

        with gr.Tab("Reminders"):
            ui_reminders()

        with gr.Tab("Applications"):
            ui_applications(demo)

        with gr.Tab("Bill Import (LLM)"):
            ui_bill_import(demo)
    return demo


if __name__ == "__main__":
    app = build_app()
    app.launch()