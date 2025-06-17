import React from 'react';

export default function SigNozPanel({ appId }) {
  const src = `https://enhanced-coral.in.signoz.cloud/home`;
  return (
    <iframe
      src={src}
      style={{ width: '100%', height: '600px', border: 0 }}
      title="SigNoz Dashboard"
    />
  );
}
