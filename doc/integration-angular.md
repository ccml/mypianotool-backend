# Brancher le scan sur la page Partition

Rien n'est encore écrit dans `mypianotool` : ce document est le mode d'emploi, et
le service ci-dessous est prêt à être déposé tel quel dans
`src/app/pages/partition/scan.service.ts`.

## Le trajet complet

```
<input type="file" accept="image/*,.pdf">   choix dans la photothèque
        │
        │  POST /api/scans            (multipart, champ « image »)
        ▼
   202 { id, etat: "en_attente" }
        │
        │  GET /api/scans/{id}        toutes les 1,5 s
        ▼
   { etat: "termine", progression: 100, rapport: { mesuresSuspectes: [...] } }
        │
        │  GET /api/scans/{id}/musicxml
        ▼
   importerMusicXML(texte)            ← `musicxml.import.ts`, déjà en place
        │
        ▼
   modèle interne → gravure → IndexedDB
```

Le point important : le backend **ne connaît rien du modèle du site**. Il rend du
MusicXML *partwise*, exactement ce que `musicxml.import.ts` sait déjà lire depuis le
Canon et la Sonate. L'import garde son compte rendu habituel (nuances, phrasés,
ornements non repris) ; le scan y ajoute seulement le sien.

## Deux comptes rendus, pas un

- celui de **l'import** dit ce que le modèle du site ne sait pas représenter ;
- celui du **scan** (`rapport`) dit ce que la reconnaissance a probablement raté.

`mesuresSuspectes` liste les mesures dont les durées ne tombent pas juste. C'est le
meilleur indice de confiance disponible : un moteur d'OMR ne fournit pas de score,
mais une mesure fausse est presque toujours une mesure mal lue. À afficher comme
« Mesures à vérifier : 7, 12, 23 », et idéalement à marquer sur la portée.

## Configuration

Ajouter l'adresse du service aux deux environnements :

```ts
// src/environments/environment.ts
export const environment = {
  production: false,
  apiScan: 'http://localhost:8077'
};

// src/environments/environment.prod.ts
export const environment = {
  production: true,
  apiScan: '/omr'        // derrière le même nom de domaine, via un proxy
};
```

En production, `/omr` est porté par **Nginx Proxy Manager**, le reverse proxy qui
est déjà devant le site : une *custom location* sur le Proxy Host existant, avec une
réécriture pour retirer le préfixe. Pas de proxy à ajouter dans `server-hybrid.js`,
donc pas de dépendance de plus. Le pas à pas est dans
[`deploiement.md`](deploiement.md).

Même domaine, donc aucun en-tête CORS n'entre en jeu — c'est la raison principale de
ce choix plutôt qu'un sous-domaine.

## Le service

```ts
import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';

/** Compte rendu de reconnaissance, tel que le renvoie le backend. */
export interface RapportScan {
  parties: number;
  mesures: number;
  notes: number;
  accords: number;
  silences: number;
  liaisons: number;
  alterations: number;
  chiffrage: string | null;
  armure: number | null;
  mesuresSuspectes: number[];
  mesuresSuspectesTotal: number;
  avertissements: string[];
}

export interface EtatScan {
  id: string;
  nomFichier: string;
  etat: 'en_attente' | 'en_cours' | 'termine' | 'echoue';
  progression: number;
  etape: string;
  erreur: string | null;
  rapport: RapportScan | null;
  remarques: string[];
  musicxml: string | null;
  apercu: string | null;
  positionFile?: number | null;
}

/**
 * Reconnaissance d'une photo de partition par le service `mypianotool-backend`.
 * Le scan est long (de une à plusieurs minutes) : on dépose, puis on interroge.
 * Le résultat est du MusicXML, qui repart dans `musicxml.import.ts` comme un
 * fichier ouvert à la main.
 */
@Injectable({ providedIn: 'root' })
export class ScanService {

  private readonly http = inject(HttpClient);
  private readonly base = environment.apiScan ?? '';

  /** État du scan en cours, pour l'affichage ; null quand il n'y en a pas. */
  readonly etat = signal<EtatScan | null>(null);

  private annule = false;

  /** true si le service répond et si ses modèles sont en place. */
  async disponible(): Promise<boolean> {
    try {
      const sante = await firstValueFrom(
        this.http.get<{ etat: string; moteurPret: boolean }>(`${this.base}/api/sante`)
      );
      return sante.etat === 'ok' && sante.moteurPret;
    } catch {
      return false;
    }
  }

  /**
   * Envoie l'image et attend le MusicXML. Rejette avec un message en clair si la
   * reconnaissance échoue ; c'est ce message qui est montré à l'utilisateur.
   */
  async scanner(fichier: File): Promise<string> {
    this.annule = false;

    const corps = new FormData();
    corps.append('image', fichier, fichier.name);

    let etat = await firstValueFrom(
      this.http.post<EtatScan>(`${this.base}/api/scans`, corps)
    );
    this.etat.set(etat);

    while (etat.etat === 'en_attente' || etat.etat === 'en_cours') {
      if (this.annule) {
        void firstValueFrom(this.http.delete(`${this.base}/api/scans/${etat.id}`)).catch(() => {});
        throw new Error('Scan annulé.');
      }
      await new Promise(r => setTimeout(r, 1500));
      etat = await firstValueFrom(this.http.get<EtatScan>(`${this.base}/api/scans/${etat.id}`));
      this.etat.set(etat);
    }

    if (etat.etat === 'echoue' || !etat.musicxml) {
      throw new Error(etat.erreur ?? 'La reconnaissance a échoué.');
    }

    return firstValueFrom(
      this.http.get(`${this.base}${etat.musicxml}`, { responseType: 'text' })
    );
  }

  /** Demande l'arrêt du scan en cours. */
  annuler(): void {
    this.annule = true;
  }

  /** Oublie le scan terminé et libère la place côté serveur. */
  async oublier(): Promise<void> {
    const courant = this.etat();
    this.etat.set(null);
    if (courant) {
      await firstValueFrom(this.http.delete(`${this.base}/api/scans/${courant.id}`)).catch(() => {});
    }
  }
}
```

## Côté composant

```ts
async surFichierChoisi(evenement: Event): Promise<void> {
  const fichier = (evenement.target as HTMLInputElement).files?.[0];
  if (!fichier) return;

  try {
    const musicxml = await this.scan.scanner(fichier);
    const resultat = importerMusicXML(musicxml);     // l'import existant
    this.chargerPartition(resultat.partition);
    this.compteRendu.set({
      ...resultat.compteRendu,
      mesuresAVerifier: this.scan.etat()?.rapport?.mesuresSuspectes ?? []
    });
  } catch (erreur) {
    this.erreurScan.set((erreur as Error).message);
  }
}
```

Dans le gabarit, un `<input type="file" accept="image/*,.pdf">` suffit : sur iPhone
comme sur Android, il ouvre la photothèque. Ajouter `capture="environment"`
déclencherait l'appareil photo directement — c'est le jour où la prise de vue sera
au programme, pas avant.

## À prévoir dans l'interface

- **La durée.** Une à plusieurs minutes. La barre de progression et le libellé
  d'étape viennent du backend (`progression`, `etape`) ; `positionFile` dit combien
  de scans passent avant.
- **Le service peut être absent.** Le site tient hors ligne ; le bouton « Scanner »
  doit se désactiver proprement quand `disponible()` répond faux, pas faire planter
  la page.
- **Le résultat est à relire.** Amener l'utilisateur sur la première mesure
  suspecte plutôt que de présenter le scan comme un import réussi : c'est la
  différence entre un outil utile et un outil qui ment.
