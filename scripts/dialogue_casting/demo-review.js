'use strict';

function validateDemoReview(doc, data, all) {
  if (!doc || typeof doc !== 'object' || doc.schema !== 'ducai-dialogue-casting-demo-review-v1'
      || doc.label_source !== 'demo-assisted-review'
      || doc.demo_id !== data.demo_id || doc.experiment_id !== data.experiment_id
      || !Array.isArray(doc.reviews)) {
    throw Error('Tệp không thuộc đúng bản demo này. Nhãn nghe gốc dùng trang gán nhãn ban đầu.');
  }
  const incoming = {};
  for (const row of doc.reviews) {
    if (!row || typeof row !== 'object') throw Error('Dòng nhãn trong JSON không hợp lệ.');
    const cue = all.get(row.cue_id);
    if (!cue || Object.hasOwn(incoming, row.cue_id) || cue.clip_sha256 !== row.clip_sha256
        || cue.machine.label !== row.machine_label
        || ![null, 'male', 'female', 'unknown'].includes(row.override)
        || typeof row.reviewed !== 'boolean' || typeof row.notes !== 'string'
        || row.notes.length > 20000) {
      throw Error('Tệp chứa câu lặp, nhãn sai hoặc âm thanh không khớp. Chưa nhập thay đổi nào.');
    }
    incoming[row.cue_id] = {override:row.override, reviewed:row.reviewed, notes:row.notes};
  }
  return incoming;
}

function snapshotDemoReview(data, all, reviews) {
  return {
    schema:'ducai-dialogue-casting-demo-review-v1',
    label_source:'demo-assisted-review',
    demo_id:data.demo_id,
    experiment_id:data.experiment_id,
    created_at:new Date().toISOString(),
    reviews:Object.entries(reviews).map(([id, review]) => ({
      cue_id:id,
      clip_sha256:all.get(id).clip_sha256,
      machine_label:all.get(id).machine.label,
      ...review
    }))
  };
}

function pendingDemoReview(cue, reviews) {
  return (reviews[cue.id]?.override || cue.machine.label) === 'unknown'
    && !reviews[cue.id]?.reviewed;
}

function changedDemoMachine(cue) {
  return !!cue.comparison_machine
    && cue.comparison_machine.label !== cue.machine.label;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {validateDemoReview, snapshotDemoReview, pendingDemoReview, changedDemoMachine};
}
