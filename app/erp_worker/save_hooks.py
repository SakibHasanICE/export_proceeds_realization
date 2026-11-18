"""What the robot needs from the database around the ERP's Save button.

The robot (erp_data) knows nothing about the database. It calls three hooks:
    gate()          before anything is clicked: raise SaveRefused(reason) to stop. Nothing has happened in the ERP yet.
    before_click()  just before Save is clicked: remember "a save is in progress". From this moment the entry cannot
                    be run again until the outcome is known, so a crash or a restart can never lead to a second save.
    after(state)    the outcome: saved | rejected | unconfirmed
"""


class SaveRefused(Exception):
    """The entry must not be saved (yet). The message is shown to the user."""


class DbSaveHooks:
    def __init__(self, entry_id: int, user: str | None = None):
        self.entry_id, self.user = entry_id, user

    def gate(self) -> None:
        from app.components import database
        from app.erp_worker.view import build_erp_view

        rec = database.get_realization(self.entry_id)
        if rec is None:
            raise SaveRefused("the entry no longer exists")
        if rec.get("status") == "filled":
            raise SaveRefused("the entry is already marked as saved")
        if rec.get("needs_review"):
            raise SaveRefused("the extractor marked this entry 'needs review': check the data first")
        view = build_erp_view(rec)
        if view["blockers"]:
            raise SaveRefused("; ".join(view["blockers"]))

    def before_click(self) -> None:
        from app.components import database
        database.set_erp_save_state(self.entry_id, "in_progress", note="Save is about to be clicked", by=self.user)

    def after(self, state: str, note: str = "") -> None:
        from app.components import database
        if state == "saved":
            database.mark_saved_in_erp(self.entry_id, note=note, by=self.user)
        elif state == "rejected":
            database.set_erp_save_state(self.entry_id, None, note=note or "the ERP refused to save", by=self.user)
        else:                                   # never leave an entry waiting for a manual check
            database.mark_saved_in_erp(self.entry_id, note=note, by=self.user)
