from nicegui import ui

from ..model import UiElementDocumentation


class UploadDocumentation(UiElementDocumentation, element=ui.upload):

    def main_demo(self) -> None:
        ui.upload(on_upload=lambda e: ui.notify(f'Uploaded {e.name}')).classes('max-w-full')

    def more(self) -> None:
        @self.add_markdown_demo('Upload restrictions', '''
            In this demo, the upload is restricted to a maximum file size of 1 MB.
            When a file is rejected, a notification is shown.
        ''')
        def upload_restrictions() -> None:
            ui.upload(on_upload=lambda e: ui.notify(f'Uploaded {e.name}'),
                      on_rejected=lambda: ui.notify('Rejected!'),
                      max_file_size=1_000_000).classes('max-w-full')

        @self.add_markdown_demo('Show file content', '''
            In this demo, the uploaded markdown file is shown in a dialog.
        ''')
        def show_file_content() -> None:
            from nicegui import events

            with ui.dialog().props('full-width') as dialog:
                with ui.card():
                    content = ui.markdown()

            def handle_upload(e: events.UploadEventArguments):
                text = e.content.read().decode('utf-8')
                content.set_content(text)
                dialog.open()

            ui.upload(on_upload=handle_upload).props('accept=.md').classes('max-w-full')
