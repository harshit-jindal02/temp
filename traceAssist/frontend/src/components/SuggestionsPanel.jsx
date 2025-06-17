import React, { useState, useEffect } from 'react';
import { Box, Typography, Paper } from '@mui/material';
import axios from 'axios';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export default function SuggestionsPanel({ appId }) {
  // STATE: suggestions is a SINGLE STRING
  const [suggestions, setSuggestions] = useState('');
  const [loading, setLoading] = useState(false);

  const fetchSuggestions = async () => {
    setLoading(true);
    try {
      const res = await axios.post(`http://localhost:8000/suggestions`, {
        app_id: appId
      });

      // RESPONSE PROCESSING: res.data.suggestions is a SINGLE STRING
      let rawSuggestions = res.data.suggestions || '';

      // Clean up the leading "• "
      const cleanedSuggestions = rawSuggestions.replace(/^•\s*(?=(###|-))/gm, '');
      // Or the simpler version if it works for your specific output:
      // const cleanedSuggestions = rawSuggestions.replace(/^•\s/gm, '');

      setSuggestions(cleanedSuggestions);

    } catch (err) {
      console.error('Error fetching suggestions:', err.response?.data || err.message);
      const errorMessage = err.response?.data?.detail || 'Failed to fetch AI suggestions. Check console.';
      alert(errorMessage);
      setSuggestions(''); // Reset to empty string on error
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (appId) {
      fetchSuggestions();
    }
  }, [appId]);

  if (loading) return <p>Loading AI suggestions…</p>;

  const muiRenderers = {
    h3: ({node, ...props}) => <Typography variant="h5" gutterBottom {...props} />,
    // Add other renderers as needed, e.g., for p, ul, li
    // h1: ({node, ...props}) => <Typography variant="h3" gutterBottom {...props} />,
    // h2: ({node, ...props}) => <Typography variant="h4" gutterBottom {...props} />,
    // p: ({node, ...props}) => <Typography paragraph {...props} />,
  };

  return (
    <Box my={2}>
      <Typography variant="h4" gutterBottom sx={{ mb: 2 }}>
        AI Suggestions
      </Typography>
      {/* RENDERING: Check if the STRING has content, then render with ReactMarkdown */}
      {suggestions ? (
        <Paper
          elevation={2}
          sx={{
            padding: '16px',
            '& ul': { pl: '20px', mt: 0.5, mb: 1 }, // Adjust list margins
            '& li': { mb: 0.5 },
            '& p': { mb: 1 }, // Add bottom margin to paragraphs
            '& h3, & h4, & h5, & h6': { mt: 2, mb: 1 }, // Add margin to headings
            '& pre': {
              backgroundColor: '#2d2d2d',
              color: '#f8f8f2',
              padding: '1em',
              overflowX: 'auto',
              borderRadius: '4px',
              fontSize: '0.9em',
              mt: 1, // Add margin-top to pre blocks
              mb: 1, // Add margin-bottom to pre blocks
            },
            '& pre code': {
              fontFamily: 'monospace',
              backgroundColor: 'transparent',
              color: 'inherit',
            },
          }}
        >
          <ReactMarkdown
            components={muiRenderers}
            remarkPlugins={[remarkGfm]}
          >
            {suggestions}
          </ReactMarkdown>
        </Paper>
      ) : (
        <Typography>
          {loading ? 'Loading...' : 'No AI suggestions available or an error occurred.'}
        </Typography>
      )}
    </Box>
  );
}