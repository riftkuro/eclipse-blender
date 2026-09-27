import bpy
from . import transforms as T


def bones(pair):
    obj = bpy.data.objects.get(pair['object'])
    if not obj or obj.type != 'ARMATURE':
        raise ValueError('Pair an output armature before syncing bones.')
    mapped = {m['source']: m for m in pair['tracks'] if not m['source'].startswith('@')}
    ancestors = {p.name for name in mapped for p in obj.data.bones[name].parent_recursive}
    result = []
    exported = set(mapped)
    for bone in sorted(obj.data.bones, key=lambda b: len(b.parent_recursive)):
        if bone.name in mapped or bone.name in ancestors or not bone.use_deform:
            continue
        rest = T.unpack(pair['alignment']) @ T.converted(T.source_frame(obj, bone.name, rest=True), pair['units'])
        parent = next((p.name for p in bone.parent_recursive if p.name in exported), None)
        result.append({'name': bone.name, 'source': bone.name, 'parent': parent, 'world': T.pack(rest)})
        exported.add(bone.name)
    return {'bones': result, 'mapped': {name: m['id'] for name, m in mapped.items()}}


def refresh(pairs, manifest, additions):
    result = []
    for pair in pairs:
        rig = next((r for r in manifest['rigs'] if r['id'] == pair['rig']), None)
        obj = bpy.data.objects.get(pair['object'])
        if not rig or not obj:
            continue
        mapping = {m['id']: m['source'] for m in pair['tracks']}
        mapping.update(additions.get(pair['rig'], {}))
        result.append(T.bind(obj, rig, mapping, pair['units']))
    return result
