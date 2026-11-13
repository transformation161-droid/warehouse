"""Strict API contracts. Client roles, authors, timestamps and versions are never trusted."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)


Text = str


class Site(Model):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(default='', max_length=120)
    storekeeper: str = Field(default='', max_length=120)
    pm: str = Field(default='', max_length=120)


class Question(Model):
    archived: bool = False
    position: int | None = Field(default=None, ge=0, le=100000, strict=True)
    templateId: Literal['global'] = 'global'
    section: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)


class Result(Model):
    questionId: str
    status: Literal['ok', 'fail', 'na']
    comment: str = Field(default='', max_length=4000)
    photo: str = Field(default='', max_length=200)
    title: str = ''
    section: str = ''


class Inspection(Model):
    siteId: str
    date: date
    inspector: str = Field(max_length=120)
    results: list[Result] = Field(min_length=1, max_length=200)
    checklistVersion: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class Finding(Model):
    siteId: str
    inspectionId: str | None = None
    questionId: str | None = None
    section: str = Field(default='Прочее', max_length=120)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default='', max_length=4000)
    severity: Literal['low', 'medium', 'high', 'critical'] = 'medium'
    owner: str = Field(default='', max_length=120)
    dueDate: date | None = None
    status: Literal['open', 'inprogress', 'closed'] = 'open'
    resolution: str = Field(default='', max_length=4000)
    photo: str = Field(default='', max_length=200)
    evidence: str = Field(default='', max_length=200)
    repeat: bool = False
    closedAt: str | None = None


class Inventory(Model):
    siteId: str
    date: date
    title: str = Field(min_length=1, max_length=230)
    plannedPositions: int = Field(default=0, ge=0, le=100000)
    status: Literal['draft', 'final'] = 'draft'
    stage: Literal['primary', 'confirmed', 'corrected', 'investigation', 'approved', 'recovered'] = 'primary'
    decision: str = Field(default='', max_length=4000)
    sourceHash: str = Field(default='', pattern=r'^(?:[0-9a-f]{64})?$')
    sourceName: str = Field(default='', max_length=200)


class InventoryLine(Model):
    inventoryId: str
    item: str = Field(min_length=1, max_length=500)
    sku: str = Field(default='', max_length=120)
    category: str = Field(default='Прочее', max_length=120)
    unit: str = Field(default='шт.', max_length=30)
    expected: float = Field(ge=0, le=100000000)
    actual: float | None = Field(default=None, ge=0, le=100000000)
    price: float = Field(ge=0, le=100000000)
    owner: str = Field(default='', max_length=120)
    reason: str = Field(default='', max_length=4000)
    sourceId: str = Field(default='', max_length=120)
    inventoryNumber: str = Field(default='', max_length=120)
    brand: str = Field(default='', max_length=120)
    sourceRow: int = Field(default=0, ge=0, le=100000)
    photo: str = Field(default='', max_length=200)
    countedBy: str = Field(default='', max_length=80)
    countedAt: str = Field(default='', max_length=50)


class Visit(Model):
    siteId: str
    date: date
    kind: Literal['inspection', 'followup', 'inventory', 'training'] = 'inspection'
    assignee: str = Field(default='', max_length=120)
    assignedTo: str = 'inspector'
    goal: str = Field(default='', max_length=1200)
    status: Literal['planned', 'completed', 'cancelled'] = 'planned'
    inspectionId: str | None = None
    cancellationReason: str = Field(default='', max_length=1200)
    cancelledBy: str | None = None
    cancelledAt: str | None = None


MODELS = {'sites': Site, 'questions': Question, 'inspections': Inspection,
          'findings': Finding, 'inventories': Inventory, 'inventoryLines': InventoryLine, 'visits': Visit}


class Change(Model):
    collection: Literal['sites', 'questions', 'inspections', 'findings', 'inventories', 'inventoryLines', 'visits']
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    expectedVersion: int = Field(ge=0)
    data: dict


class Changes(Model):
    mutationId: str = Field(min_length=12, max_length=100)
    changes: list[Change] = Field(min_length=1, max_length=2000)


class Login(Model):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class ImportFile(Model):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4_100_000)
    mapping: dict[str, str] | None = None


class InventoryPlanUpload(ImportFile):
    siteId: str = Field(min_length=1, max_length=80)
    date: date
    title: str = Field(min_length=1, max_length=230)


class InventoryCountUpdate(Model):
    expectedVersion: int = Field(ge=1)
    actual: float | None = Field(default=None, ge=0, le=100000000)
    reason: str = Field(default='', max_length=4000)
    photo: str = Field(default='', max_length=200)


class InventoryFinalize(Model):
    expectedVersion: int = Field(ge=1)


class DecisionPreview(Model):
    sheet: int = Field(default=0, ge=0, le=19)
    headerRow: int = Field(default=1, ge=1, le=99999)
    firstRow: int = Field(default=2, ge=2, le=100000)
    lastRow: int = Field(default=100000, ge=2, le=100000)
    mapping: dict[str, str] = Field(default_factory=dict, max_length=17)
    allowCached: bool = False
    reportPeriod: str = Field(default='', pattern=r'^(?:|[0-9]{4}-Q[1-4])(Model):
    siteId: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4_100_000)
    mutationId: str | None = Field(default=None,min_length=12,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')


class OfflinePackage(Model):
    siteIds: list[str] = Field(min_length=1,max_length=5)
    ttlHours: int = Field(default=24,ge=1,le=24)


class OfflineChanges(Changes):
    packageId: str = Field(pattern=r'^[0-9a-f]{48}$')
    changes: list[Change] = Field(min_length=1,max_length=100)


class DecisionVersionPreview(Model):
    documentId: str = Field(min_length=1,max_length=80)
    expectedVersion: int = Field(ge=1)
    mode: Literal['full','partial']
    reason: str = Field(min_length=1,max_length=1200)
    targetIds: list[str] = Field(default_factory=list,max_length=50000)
    matches: dict[str,str] = Field(default_factory=dict,max_length=50000)


class DecisionVersionConfirm(Model):
    token: str = Field(min_length=64,max_length=64)
)


class MediaFile(Model):
    siteId: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4_100_000)
    mutationId: str | None = Field(default=None,min_length=12,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')


class OfflinePackage(Model):
    siteIds: list[str] = Field(min_length=1,max_length=5)
    ttlHours: int = Field(default=24,ge=1,le=24)


class OfflineChanges(Changes):
    packageId: str = Field(pattern=r'^[0-9a-f]{48}$')
    changes: list[Change] = Field(min_length=1,max_length=100)


class DecisionVersionPreview(Model):
    documentId: str = Field(min_length=1,max_length=80)
    expectedVersion: int = Field(ge=1)
    mode: Literal['full','partial']
    reason: str = Field(min_length=1,max_length=1200)
    targetIds: list[str] = Field(default_factory=list,max_length=50000)
    matches: dict[str,str] = Field(default_factory=dict,max_length=50000)


class DecisionVersionConfirm(Model):
    token: str = Field(min_length=64,max_length=64)
