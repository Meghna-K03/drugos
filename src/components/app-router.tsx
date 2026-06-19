'use client';

import { createContext, useContext, useCallback, useState, ReactNode } from 'react';

interface DrugOSNavContextType {
  activeSection: string;
  navigate: (section: string) => void;
}

const DrugOSNavContext = createContext<DrugOSNavContextType>({
  activeSection: 'billing-subscription',
  navigate: () => {},
});

export function DrugOSNavProvider({ children, initialSection }: { children: ReactNode; initialSection?: string }) {
  const [activeSection, setActiveSection] = useState(initialSection ?? 'billing-subscription');
  const navigate = useCallback((section: string) => setActiveSection(section), []);
  return (
    <DrugOSNavContext.Provider value={{ activeSection, navigate }}>
      {children}
    </DrugOSNavContext.Provider>
  );
}

export function useDrugOSNav() {
  return useContext(DrugOSNavContext);
}
