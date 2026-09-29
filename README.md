# Eclipse Blender 1.5.2

Blender companion for Eclipse Animator. This is the corrected 1.5.2 release.

Use Eclipse rig-polish or later and export the rig again. Import through Eclipse
in Blender, keeping the OBJ, MTL and texture files together. R6 exports preserve
Roblox's character rendering, including the clothing atlas and beveled body
geometry. The classic Head conversion uses the correct scale and native normals.
Centered exports place the visible rig's lowest bounds at ground level; the
armature and all bone/rest matrices move together. Disabling Center Origin keeps
original world placement. Imported character textures are packed into the blend.

Includes the 1.5.1 armature export feature. Existing MeshPart/image materials and
old OBJ metadata remain supported, but old exports cannot recover clothing that
was never exported. Re-export for the complete correction. Native R6 heads use the actual composited face in the exported character atlas,
with square head UVs. Missing character atlases produce an import error rather
than substituting a default smile. Legacy non-character default-face fallback
also uses the corrected square UV mapping.

https://discord.gg/eclipseanimator


1.5.2 avatar appearance correction: current Eclipse exports preload the complete native avatar in Workspace before opening Save. Import preserves Studio-rendered custom meshes, CharacterMesh/R6/R15 surfaces, UVs, clothing, accessories and face textures; it packs exported images. Keep each OBJ with its MTL and image files. No template head/material replacement is applied to new native-rendered exports. Old metadata remains supported.
