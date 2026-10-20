# Eclipse Blender 1.5.16

Blender companion for Eclipse Animator. 

Use **Copy Camera FOV** in the Eclipse panel to copy the evaluated perspective
camera's vertical field of view over the scene's frame range. If the scene has
multiple cameras, select one first.

In Eclipse Animator, choose **Add Blender Camera FOV** below **Add Camera**, or
use the same button in the Blender window's rig tools. Paste the text and press
**Save**. Select a timeline camera first if your project has more than one.
The import starts at frame zero, converts source timing to the project's FPS,
and supports undo. Camera position and rotation are preserved.

Lens shift, orthographic projection, and vertical FOV outside Roblox's 1–120°
range are rejected with an explanation.

https://discord.gg/eclipseanimator
