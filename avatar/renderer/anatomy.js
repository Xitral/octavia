// Where things are on her, measured from the model itself rather than assumed: her eyes as they show.

import * as THREE from 'three';

// Where an eye shows, in world space as she stands now: the middle of the vertices skinned mostly to its eye bone
// (iris, white and highlight), because on VRoid models the bone itself sits deep in the head, toward the nose.
// Falls back to the bone when no vertex follows it; null without an eye bone.
export function eyeWorld(vrm, side) {
  const eye = vrm?.humanoid?.getRawBoneNode?.(`${side}Eye`);
  if (!eye) return null;
  vrm.scene.updateMatrixWorld(true);
  const sum = new THREE.Vector3();
  const v = new THREE.Vector3();
  let count = 0;
  vrm.scene.traverse((mesh) => {
    if (!mesh.isSkinnedMesh) return;
    const bone = mesh.skeleton.bones.indexOf(eye);
    if (bone < 0) return;
    const { skinIndex, skinWeight } = mesh.geometry.attributes;
    for (let i = 0; i < skinIndex.count; i++) {
      let weight = 0;
      for (let k = 0; k < 4; k++) if (skinIndex.getComponent(i, k) === bone) weight += skinWeight.getComponent(i, k);
      if (weight < 0.5) continue;
      sum.add(mesh.getVertexPosition(i, v).applyMatrix4(mesh.matrixWorld));
      count++;
    }
  });
  return count ? sum.divideScalar(count) : eye.getWorldPosition(sum);
}

// The middle of her two eyes as they show (world), or the one she has, or null without any eye bones.
export function eyesWorld(vrm) {
  const eyes = ['left', 'right'].map((side) => eyeWorld(vrm, side)).filter(Boolean);
  if (!eyes.length) return null;
  return eyes.reduce((sum, e) => sum.add(e), new THREE.Vector3()).divideScalar(eyes.length);
}
