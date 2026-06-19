'use client';

import { useState, useMemo, useRef } from 'react';
import { Search, X, Clock, ArrowRight } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { diseases } from '@/lib/mock-data';

interface DiseaseSearchBarProps {
  onSearch?: (query: string) => void;
  onDiseaseSelect?: (diseaseId: string) => void;
  placeholder?: string;
  className?: string;
}

const recentSearches = [
  "Huntington's Disease",
  "Alzheimer's Disease",
  'ALS',
  'Cystic Fibrosis',
];

export function DiseaseSearchBar({
  onSearch,
  onDiseaseSelect,
  placeholder = 'Search diseases, drugs, genes, pathways...',
  className = '',
}: DiseaseSearchBarProps) {
  const [query, setQuery] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const suggestions = useMemo(() => {
    if (query.length < 2) return [];
    const lower = query.toLowerCase();
    return diseases
      .filter(
        (d) =>
          d.name.toLowerCase().includes(lower) ||
          d.synonyms.some((s) => s.toLowerCase().includes(lower)) ||
          d.category.toLowerCase().includes(lower)
      )
      .slice(0, 6);
  }, [query]);

  const handleSelect = (diseaseId: string) => {
    setQuery('');
    setIsFocused(false);
    onDiseaseSelect?.(diseaseId);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (query.trim()) {
      onSearch?.(query.trim());
      setIsFocused(false);
    }
  };

  const showDropdown = isFocused && (suggestions.length > 0 || (query.length === 0 && recentSearches.length > 0));

  return (
    <div className={`relative ${className}`}>
      <form onSubmit={handleSubmit} className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setIsFocused(true)}
          onBlur={() => setTimeout(() => setIsFocused(false), 200)}
          placeholder={placeholder}
          className="pl-10 pr-10 h-11 bg-white border-border focus:border-primary focus:ring-primary"
        />
        {query && (
          <button
            type="button"
            onClick={() => setQuery('')}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </form>

      {showDropdown && (
        <div className="absolute z-50 w-full mt-1 bg-popover border border-border rounded-lg shadow-lg overflow-hidden">
          {query.length === 0 && recentSearches.length > 0 && (
            <div className="p-3">
              <p className="text-xs font-medium text-muted-foreground mb-2">Recent Searches</p>
              <div className="space-y-1">
                {recentSearches.map((search) => (
                  <button
                    key={search}
                    onClick={() => {
                      setQuery(search);
                      onSearch?.(search);
                      setIsFocused(false);
                    }}
                    className="flex items-center gap-2 w-full px-2 py-1.5 text-sm rounded-md hover:bg-accent text-left"
                  >
                    <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                    <span>{search}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {suggestions.length > 0 && (
            <div className="border-t border-border p-2">
              <p className="text-xs font-medium text-muted-foreground px-2 mb-1">Diseases</p>
              {suggestions.map((disease) => (
                <button
                  key={disease.id}
                  onClick={() => handleSelect(disease.id)}
                  className="flex items-center justify-between w-full px-2 py-2 text-sm rounded-md hover:bg-accent text-left"
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-medium truncate">{disease.name}</div>
                    <div className="text-xs text-muted-foreground">{disease.category} · {disease.candidateCount} candidates</div>
                  </div>
                  <div className="flex items-center gap-2 ml-2">
                    <Badge variant="secondary" className="text-xs shrink-0">
                      {disease.icd10}
                    </Badge>
                    <ArrowRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
