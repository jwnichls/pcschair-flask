"""Models mirroring the Rails schema, so an existing pcschair database can be reused."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Uuid, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db


def utcnow():
    # Rails stores timestamps as naive UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(dt):
    """Format like Rails' JSON timestamps, e.g. 2026-10-27T23:31:00.000Z."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def find_venue(venue_id):
    try:
        venue_uuid = uuid.UUID(str(venue_id))
    except (TypeError, ValueError):
        return None
    return db.session.get(Venue, venue_uuid)


class Venue(db.Model):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String)
    active_paper: Mapped[bool | None] = mapped_column(Boolean)
    timer: Mapped[datetime | None] = mapped_column(DateTime)
    breaktime: Mapped[bool | None] = mapped_column(Boolean)
    paper_title: Mapped[str | None] = mapped_column(String)
    paper_authors: Mapped[str | None] = mapped_column(Text)
    paper_pcs_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    pcs2_flag: Mapped[bool | None] = mapped_column(Boolean)
    pcs2_venue_name: Mapped[str | None] = mapped_column(String)
    allow_adds: Mapped[bool | None] = mapped_column(Boolean, default=True)
    sub_committee: Mapped[bool | None] = mapped_column(Boolean, default=False)
    sub_committee_id: Mapped[int | None] = mapped_column(Integer, default=0)

    papers: Mapped[list["Paper"]] = relationship(
        back_populates="venue", order_by="Paper.listorder", cascade="all, delete-orphan"
    )

    def to_json(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "active_paper": self.active_paper,
            "timer": iso_utc(self.timer),
            "breaktime": self.breaktime,
            "paper_title": self.paper_title,
            "paper_authors": self.paper_authors,
            "paper_pcs_id": self.paper_pcs_id,
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
            "pcs2_flag": self.pcs2_flag,
            "pcs2_venue_name": self.pcs2_venue_name,
            "allow_adds": self.allow_adds,
            "sub_committee": self.sub_committee,
            "sub_committee_id": self.sub_committee_id,
        }

    def queued_paper(self, pcs_paper_id):
        return next((p for p in self.papers if p.pcs_paper_id == pcs_paper_id), None)


class Paper(db.Model):
    __tablename__ = "papers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    venue_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("venues.id"))
    pcs_paper_id: Mapped[int | None] = mapped_column(Integer)
    listorder: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    venue: Mapped[Venue | None] = relationship(back_populates="papers")

    def to_json(self):
        return {
            "id": str(self.id),
            "venue_id": str(self.venue_id),
            "pcs_paper_id": self.pcs_paper_id,
            "listorder": self.listorder,
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }

    def _neighbors(self, condition, order):
        return db.session.scalars(
            select(Paper).where(Paper.venue_id == self.venue_id, condition).order_by(order).limit(2)
        ).all()

    def move_up(self):
        above = self._neighbors(Paper.listorder < self.listorder, Paper.listorder.desc())
        if len(above) == 1:
            self.listorder = above[0].listorder - 1.0
        elif len(above) == 2:
            self.listorder = (above[0].listorder + above[1].listorder) / 2

    def move_down(self):
        below = self._neighbors(Paper.listorder > self.listorder, Paper.listorder.asc())
        if len(below) == 1:
            self.listorder = below[0].listorder + 1.0
        elif len(below) == 2:
            self.listorder = (below[0].listorder + below[1].listorder) / 2

    @staticmethod
    def next_listorder(venue_id):
        current = db.session.scalar(select(func.max(Paper.listorder)).where(Paper.venue_id == venue_id))
        return 1.0 if current is None else current + 1.0
