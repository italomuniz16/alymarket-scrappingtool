import { Filter, RotateCcw, SlidersHorizontal } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { EMPTY_CRITERIA } from "@/lib/api-types";
import { splitCsv, type FiltersDraft } from "@/lib/filters";

const SITUACOES = ["ATIVA", "BAIXADA", "SUSPENSA", "INAPTA", "NULA"];

export function FiltersSidebar({
  draft,
  onDraftChange,
  onApply,
}: {
  draft: FiltersDraft;
  onDraftChange: (next: FiltersDraft) => void;
  onApply: () => void;
}) {
  // Texto livre dos campos de lista (atividade/região/porte): guardado à parte do
  // valor parseado (draft.criteria.*) pra não perder o que a pessoa está digitando
  // no meio de uma vírgula -- mesma UX do st.text_input do dashboard Streamlit,
  // que também só interpreta o CSV quando lê o valor, não a cada tecla.
  const [atividadeRaw, setAtividadeRaw] = useState("");
  const [regiaoRaw, setRegiaoRaw] = useState("");
  const [porteRaw, setPorteRaw] = useState("");

  const updateCriteria = (patch: Partial<FiltersDraft["criteria"]>) =>
    onDraftChange({ ...draft, criteria: { ...draft.criteria, ...patch } });

  const handleReset = () => {
    setAtividadeRaw("");
    setRegiaoRaw("");
    setPorteRaw("");
    onDraftChange({ criteria: EMPTY_CRITERIA, demo: false, limit: 100 });
  };

  return (
    <aside className="w-full shrink-0 border-border bg-sidebar lg:sticky lg:top-16 lg:h-[calc(100vh-4rem)] lg:w-[19rem] lg:overflow-y-auto lg:border-r">
      <div className="flex flex-col gap-6 p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <SlidersHorizontal className="size-4 text-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-sidebar-foreground">Filtros ICP</h2>
          </div>
          <Button variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs" onClick={handleReset}>
            <RotateCcw className="size-3" aria-hidden="true" />
            Limpar
          </Button>
        </div>

        <div className="space-y-2">
          <Label htmlFor="pais">País</Label>
          <Select
            value={typeof draft.criteria.pais === "string" ? draft.criteria.pais : "BR"}
            onValueChange={(v) => updateCriteria({ pais: v })}
          >
            <SelectTrigger id="pais">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="BR">BR — Brasil</SelectItem>
              <SelectItem value="FR">FR — França</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-2">
          <Label htmlFor="atividade">Atividade (CNAE / NAF)</Label>
          <Input
            id="atividade"
            placeholder="6462000, 6810202"
            value={atividadeRaw}
            onChange={(e) => {
              setAtividadeRaw(e.target.value);
              updateCriteria({ cod_atividade: splitCsv(e.target.value) });
            }}
          />
          <p className="text-xs text-muted-foreground">Separe múltiplos códigos por vírgula.</p>
        </div>

        <div className="space-y-2">
          <Label htmlFor="regiao">Região (UF / département)</Label>
          <Input
            id="regiao"
            placeholder="SP, MT, DF"
            value={regiaoRaw}
            onChange={(e) => {
              setRegiaoRaw(e.target.value);
              updateCriteria({ regiao: splitCsv(e.target.value) });
            }}
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="porte">Porte</Label>
          <Input
            id="porte"
            placeholder="ME, EPP, DEMAIS"
            value={porteRaw}
            onChange={(e) => {
              setPorteRaw(e.target.value);
              updateCriteria({ porte: splitCsv(e.target.value) });
            }}
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="situacao">Situação</Label>
          <Select
            value={typeof draft.criteria.situacao === "string" ? draft.criteria.situacao : "__all"}
            onValueChange={(v) => updateCriteria({ situacao: v === "__all" ? null : v })}
          >
            <SelectTrigger id="situacao">
              <SelectValue placeholder="Todas" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">Todas</SelectItem>
              {SITUACOES.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-2">
          <Label htmlFor="abertura">Aberta a partir de</Label>
          <Input
            id="abertura"
            type="date"
            value={draft.criteria.aberta_apos ?? ""}
            onChange={(e) => updateCriteria({ aberta_apos: e.target.value || null })}
          />
        </div>

        <div className="flex items-center gap-2">
          <Checkbox
            id="email"
            checked={draft.criteria.com_email}
            onCheckedChange={(checked) => updateCriteria({ com_email: checked === true })}
          />
          <Label htmlFor="email" className="font-normal">
            Só empresas com e-mail
          </Label>
        </div>

        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <Label htmlFor="rows">Linhas no preview</Label>
            <span className="num-tabular text-xs font-semibold text-accent">{draft.limit}</span>
          </div>
          <Slider
            id="rows"
            value={[draft.limit]}
            onValueChange={([v]) => onDraftChange({ ...draft, limit: v })}
            min={10}
            max={500}
            step={10}
            aria-label="Linhas no preview"
          />
        </div>

        <Button className="w-full gap-2" onClick={onApply}>
          <Filter className="size-4" aria-hidden="true" />
          Aplicar filtros
        </Button>

        <Separator />

        <div className="flex items-start justify-between gap-3 rounded-lg bg-secondary p-3">
          <div>
            <Label htmlFor="demo" className="text-sm">
              Modo demonstração
            </Label>
            <p className="mt-1 text-xs text-muted-foreground">
              Mostra também dados fictícios (Faker) além dos reais. Nunca habilita exportação.
            </p>
          </div>
          <Switch
            id="demo"
            checked={draft.demo}
            onCheckedChange={(checked) => onDraftChange({ ...draft, demo: checked })}
          />
        </div>
      </div>
    </aside>
  );
}
