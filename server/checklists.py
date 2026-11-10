"""One global catalog for this release; immutable content versions remain in checklist_versions.

A future template registry can resolve template_id to site assignments before this
selection. Existing package/version hashes and question IDs need no replacement.
"""
DEFAULT_TEMPLATE='global'


def ordered(questions):
    def key(q):
        position=q.get('position')
        if isinstance(position,int) and not isinstance(position,bool):return (0,position,q['id'])
        ident=q['id'];number=int(ident[1:]) if ident.startswith('q') and ident[1:].isdigit() else 1000000
        return (1,number,ident)
    return sorted(questions,key=key)


def active(questions,template_id=DEFAULT_TEMPLATE):
    if template_id!=DEFAULT_TEMPLATE:raise ValueError('Only the approved global template is enabled')
    return ordered([q for q in questions if not q.get('archived',False)])
