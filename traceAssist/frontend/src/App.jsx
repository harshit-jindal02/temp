// import React, { useState } from 'react';
// import axios from 'axios';
// import UploadForm from './components/UploadForm';
// import SuggestionsPanel from './components/SuggestionsPanel';
// import SigNozPanel from './components/SigNozPanel';
// import { Box, Typography, Button, Divider } from '@mui/material';
// import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';

// const API_BASE = 'http://localhost:8000'; // <— change here if your backend endpoint moves

// function App() {
//   const [appId, setAppId] = useState(null);
//   const [instrumented, setInstrumented] = useState(false);

//   const handleAppReady = (id) => {
//     setAppId(id);
//   };

//   const handleInstrumentAndRun = async () => {
//     try {
//       // Instrument
//       await axios.post(
//         `${API_BASE}/instrument`,
//         { app_id: appId },
//         { headers: { 'Content-Type': 'application/json' } }
//       );

//       setInstrumented(true);

//       // Run
//       await axios.post(
//         `${API_BASE}/run`,
//         { app_id: appId },
//         { headers: { 'Content-Type': 'application/json' } }
//       );
//     } catch (err) {
//       console.error(err.response?.data || err.message);
//       alert('Failed to instrument or run the application. Check console for details.');
//     }
//   };

//   return (
//     <Box
//       height={'100vh'}
//       width={'100vw'}
//       display={'flex'}
//       sx={{
//         background: 'linear-gradient(135deg, #232b5d 0%, #3e6b89 40%, #4fd1c5 100%)',
//       }}
//     >
//       {/* Sidebar */}
//       <Box
//         width={'19%'}
//         display={'flex'}
//         flexDirection={'column'}
//         pt={'3rem'}
//         sx={{
//           background: 'linear-gradient(135deg, #232b5d 0%, #3e6b89 100%)',
//           boxShadow: 3
//         }}
//       >
//         {/* Logo & Title */}
//         <Box
//           sx={{
//             mb: 3,
//             display: 'flex',
//             alignItems: 'center',
//             flexDirection: 'column',
//           }}
//         >
//           <Box sx={{ mb: 1 }}>
//             <svg width="125" height="125" viewBox="0 0 48 48" fill="none">
//               <circle cx="24" cy="24" r="22" fill="#4fd1c5" stroke="#fff" strokeWidth="3" />
//               <path d="M24 14v10l7 7" stroke="#232b5d" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
//               <circle cx="24" cy="24" r="4" fill="#fff" stroke="#232b5d" strokeWidth="2" />
//             </svg>
//           </Box>
//           <Typography variant="h6" color="#fff" fontWeight={700} letterSpacing={1} sx={{ mb: 0.5 }}>
//             Trace Assist
//           </Typography>
//           <Typography variant="caption" color="#b0bec5" align="center" sx={{ px: 1 }}>
//             AI-Powered Observability
//           </Typography>
//         </Box>
//         <Divider sx={{ width: '100%', mb: 2, bgcolor: '#4fd1c5' }} />        
//       </Box>

//       {/* Main Content */}
//       <Box
//         width={'81vw'}
//         flex={1}
//         display="flex"
//         alignItems="center"
//         justifyContent="center"
//         height={'100vh'}
//       >
//         <Box
//           sx={{
//             borderRadius: 5,
//             p: 4,
//             background: 'rgba(255,255,255,0.97)',
//             boxShadow: '0 8px 32px rgba(44, 62, 80, 0.13)',
//             backdropFilter: 'blur(6px)',
//             width: appId ? '90%' : '700'
//           }}
//         >
//           <Typography
//             variant="h3"
//             fontWeight={700}
//             gutterBottom
//             sx={{
//               background: 'linear-gradient(90deg, #232b5d 0%, #4fd1c5 100%)',
//               WebkitBackgroundClip: 'text',
//               WebkitTextFillColor: 'transparent',
//               mb: 2,
//             }}
//           >
//             Trace Assist Dashboard
//           </Typography>
//           <Typography variant="subtitle1" color="text.secondary" sx={{ mb: 3 }}>
//             Accelerate observability with automated, AI-powered instrumentation for your applications.
//           </Typography>

//           {/* Step 1: Upload or Clone */}
//           {!appId && (
//             <UploadForm apiBase={API_BASE} onAppReady={handleAppReady} />
//           )}

//           {/* Step 2: Instrument & Run */}
//           {appId && !instrumented && (
//             <Button
//               variant="contained"
//               size="large"
//               startIcon={<AutoAwesomeIcon />}
//               sx={{
//                 mt: 4,
//                 background: 'linear-gradient(90deg, #232b5d 0%, #4fd1c5 100%)',
//                 color: '#fff',
//                 fontWeight: 600,
//                 letterSpacing: 1,
//                 px: 4,
//                 py: 1.5,
//                 borderRadius: 3,
//                 boxShadow: '0 4px 16px rgba(44, 62, 80, 0.10)',
//                 textTransform: 'none',
//                 fontSize: '1.15rem',
//                 '&:hover': {
//                   background: 'linear-gradient(90deg, #1e224d 0%, #3e6b89 100%)',
//                 },
//               }}
//               onClick={handleInstrumentAndRun}
//             >
//               Instrument & Run
//             </Button>
//           )}

//           {/* Step 3: AI Suggestions & SigNoz */}
//           {instrumented && (
//             <>
//               <Box>
//                 <SuggestionsPanel apiBase={API_BASE} appId={appId} />
//               </Box>
//               <Box mt={2}>
//                 <SigNozPanel appId={appId} />
//               </Box>
//             </>
//           )}

//         </Box>
//       </Box>
//     </Box>
//   );
// }

// export default App;

import React, { useState, useEffect } from 'react';
import axios from 'axios';
import UploadForm from './components/UploadForm';
import SuggestionsPanel from './components/SuggestionsPanel';
import GrafanaPanel from './components/SigNozPanel';
import {
  Box, Typography, Button, Divider, List, ListItem, ListItemIcon, ListItemText,
  CircularProgress, Paper
} from '@mui/material';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorIcon from '@mui/icons-material/Error';
import HourglassEmptyIcon from '@mui/icons-material/HourglassEmpty';

function App() {
  const [appId, setAppId] = useState(null);
  const [instrumented, setInstrumented] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [processSteps, setProcessSteps] = useState([]);

  useEffect(() => {
    if (isLoading) {
      console.log('[EFFECT] isLoading is true. processSteps:', JSON.stringify(processSteps));
    }
  }, [isLoading, processSteps]);


  const handleAppReady = (id) => {
    setAppId(id);
    setInstrumented(false);
    setProcessSteps([]);
    setIsLoading(false);
  };

  const handleInstrumentAndRun = async () => {

    if (!appId) return;

    setInstrumented(false);
    setIsLoading(true);

    // Initial steps: validate/instrument are success, run is loading, finalize is pending
    setProcessSteps([
      { key: 'validate', label: 'Validating upload...', status: 'success' },
      { key: 'instrument', label: 'Instrumenting application...', status: 'loading' },
      { key: 'run', label: 'Running instrumented application...', status: 'loading' },
      { key: 'finalize', label: 'Finalizing...', status: 'pending' }
    ]);

    try {
      // Instrument step (already marked as success in UI)
        await axios.post(
          'http://localhost:8000/instrument',
          { app_id: appId },
          { headers: { 'Content-Type': 'application/json' } }
        );

        // Run step: update 'run' to loading
        setProcessSteps([
          { key: 'validate', label: 'Validating upload...', status: 'success' },
          { key: 'instrument', label: 'Instrumenting application...', status: 'success' },
          { key: 'run', label: 'Running instrumented application...', status: 'loading' },
          { key: 'finalize', label: 'Finalizing...', status: 'pending' }
        ]);

        await axios.post(
          'http://localhost:8000/run',
          { app_id: appId },
          { headers: { 'Content-Type': 'application/json' } }
        );

        // Finalize step: update 'run' to success, 'finalize' to loading
        setProcessSteps([
          { key: 'validate', label: 'Validating upload...', status: 'success' },
          { key: 'instrument', label: 'Instrumenting application...', status: 'success' },
          { key: 'run', label: 'Running instrumented application...', status: 'success' },
          { key: 'finalize', label: 'Finalizing...', status: 'loading' }
        ]);

        // Simulate finalizing step
        await new Promise(resolve => setTimeout(resolve, 700));

        // All steps done: mark all as success
        setProcessSteps([
          { key: 'validate', label: 'Validating upload...', status: 'success' },
          { key: 'instrument', label: 'Instrumenting application...', status: 'success' },
          { key: 'run', label: 'Running instrumented application...', status: 'success' },
          { key: 'finalize', label: 'Finalizing...', status: 'success' }
        ]);

        setInstrumented(true);

      } catch (err) {
        console.error("Error in handleInstrumentAndRun:", err.response?.data || err.message);

        setProcessSteps(prevSteps =>
          prevSteps.map(step => {
            if (step.status === 'loading') {
              return { ...step, label: `${step.label.replace('...', '')} Failed.`, status: 'error' };
            }
            return step;
          })
        );
        setInstrumented(false);
      } finally {
        setIsLoading(false);
      }
  };

  if (appId && !instrumented) {
    console.log(`[RENDER] appId && !instrumented block. isLoading: ${isLoading}, processSteps.length: ${processSteps.length}`);
  }

  return (
    <Box
      height={'100vh'}
      width={'100vw'}
      display={'flex'}
      sx={{
        background: 'linear-gradient(135deg, #232b5d 0%, #3e6b89 40%, #4fd1c5 100%)',
        overflow: 'hidden',
      }}
    >
      {/* Sidebar */}
      <Box
        width={'19%'}
        display={'flex'}
        flexDirection={'column'}
        pt={'3rem'}
        sx={{
          background: 'linear-gradient(135deg, #232b5d 0%, #3e6b89 100%)',
          boxShadow: 3,
          overflowY: 'auto',
        }}
      >
        <Box
          sx={{
            mb: 3,
            display: 'flex',
            alignItems: 'center',
            flexDirection: 'column',
          }}
        >
          <Box sx={{ mb: 1 }}>
            <svg width="125" height="125" viewBox="0 0 48 48" fill="none">
              <circle cx="24" cy="24" r="22" fill="#4fd1c5" stroke="#fff" strokeWidth="3" />
              <path d="M24 14v10l7 7" stroke="#232b5d" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
              <circle cx="24" cy="24" r="4" fill="#fff" stroke="#232b5d" strokeWidth="2" />
            </svg>
          </Box>
          <Typography variant="h6" color="#fff" fontWeight={700} letterSpacing={1} sx={{ mb: 0.5 }}>
            Insight Assist
          </Typography>
          <Typography variant="caption" color="#b0bec5" align="center" sx={{ px: 1 }}>
            AI-Powered Observability
          </Typography>
        </Box>
        <Divider sx={{ width: '100%', mb: 2, bgcolor: '#4fd1c5' }} />
      </Box>

      {/* Main Content */}
      <Box
        width={'81vw'}
        flex={1}
        display="flex"
        flexDirection="column"
        alignItems="center"
        justifyContent="flex-start"
        pt={2}
        height={'100vh'}
        sx={{ overflowY: 'auto' }}
      >
        <Box
          sx={{
            borderRadius: 2,
            p: appId ? 3 : 4,
            background: 'rgba(255,255,255,0.97)',
            boxShadow: '0 8px 32px rgba(44, 62, 80, 0.13)',
            backdropFilter: 'blur(6px)',
            width: appId ? '95%' : '700px',
            maxWidth: '1200px',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            my: 2,
          }}
        >
          <Typography
            variant="h3"
            fontWeight={700}
            gutterBottom
            align="center"
            sx={{
              background: 'linear-gradient(90deg, #232b5d 0%, #4fd1c5 100%)',
              WebkitBackgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
              mb: 1,
            }}
          >
            Insight Assist Dashboard
          </Typography>
          <Typography variant="subtitle1" color="text.secondary" align="center" sx={{ mb: 3 }}>
            Accelerate observability with automated, AI-powered instrumentation for your applications.
          </Typography>

          {!appId && (
            <UploadForm onAppReady={handleAppReady} />
          )}

          {appId && !instrumented && (
            <Box display="flex" flexDirection="column" alignItems="center" width="100%" mt={1}>
              <Button
                variant="contained"
                size="large"
                startIcon={isLoading ? <CircularProgress size={24} color="inherit" /> : <AutoAwesomeIcon />}
                disabled={isLoading}
                sx={{
                  background: 'linear-gradient(90deg, #232b5d 0%, #4fd1c5 100%)',
                  color: '#fff',
                  fontWeight: 600,
                  letterSpacing: 1,
                  px: 4, py: 1.5, borderRadius: 3,
                  boxShadow: '0 4px 16px rgba(44, 62, 80, 0.10)',
                  textTransform: 'none', fontSize: '1.15rem',
                  '&:hover': { background: 'linear-gradient(90deg, #1e224d 0%, #3e6b89 100%)' },
                  '&:disabled': { background: 'grey', color: '#ccc' }
                }}
                onClick={handleInstrumentAndRun}
              >
                {isLoading ? 'Processing...' : 'Instrument & Run'}
              </Button>
              

              {isLoading && processSteps.length > 0 && (
                <Paper
                  elevation={2}
                  variant="outlined"
                  sx={{
                    mt: 3,
                    p: 1.5,
                    width: '100%',
                    maxWidth: '480px',
                    borderRadius: '8px',
                    background: 'rgba(240,245,255,0.9)',
                    borderColor: 'rgba(0,0,0,0.15)',
                  }}
                >
                  <List dense>
                    {processSteps.map((step) => (
                      <ListItem key={step.key} sx={{ py: 0.5 }}>
                        <ListItemIcon sx={{ minWidth: '36px', display: 'flex', alignItems: 'center' }}>
                          {step.status === 'loading' && <CircularProgress size={20} color="primary" />}
                          {step.status === 'success' && <CheckCircleIcon sx={{ color: 'success.main' }} />}
                          {step.status === 'error' && <ErrorIcon sx={{ color: 'error.main' }} />}
                          {step.status === 'pending' && <HourglassEmptyIcon sx={{ color: 'action.disabled' }} />}
                        </ListItemIcon>
                        <ListItemText
                          primary={step.label}
                          slotProps={{
                            primary: {
                              variant: 'body2',
                              sx: {
                                color: step.status === 'error' ? 'error.main' : step.status === 'pending' ? 'text.secondary' : 'text.primary',
                                fontWeight: step.status === 'loading' || step.status === 'success' ? 500 : 400,
                              }
                            }
                          }}
                        />
                      </ListItem>
                    ))}
                  </List>
                </Paper>
              )}

            </Box>
          )}

          {instrumented && (
            <Box width="100%" mt={2} display="flex" flexDirection="column" gap={2}>
              <SuggestionsPanel appId={appId} />
              <GrafanaPanel dashboardUid="YOUR_DASHBOARD_UID_HERE" />
            </Box>
          )}
        </Box>
      </Box>
    </Box>
  );
}

export default App;